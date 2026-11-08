#!/bin/bash
# ゲームマニュアルの OCR XML を、game_manual_schema.xsd に沿ってマークアップする。
#
# RCGSa+数字7桁が1つのゲーム（1つの manual）です。
# 冊子コード（m001, m002, p001...）、その中の3桁ページ番号の順に page を並べます。
# 同じページの XML が複数あるときは _converted.xml を使います。
#
# 地のテキストは改行の連結以外いじらないようモデルに求める。
# 地の文がずれてもページは保存し、差分をログに残して次へ進む。見出しが無い区間には head を付けません。
# Gemini が空応答、タイムアウト、途中で切れた応答を返した場合は、
# 完了済みの page だけを残して止まります。同じコマンドを再実行すると続きから処理します。
#
# 使い方:
#   ./markup_manual.sh
#   ./markup_manual.sh RCGSa0000002
#   ./markup_manual.sh --from RCGSa0000100
#   ./markup_manual.sh --pages-per-call 4 --max-pages 8
#   ./markup_manual.sh --dry-run
#
# 認証は Google AI Studio の API キー（GEMINI_API_KEY）だけを使う。
# 規則と XSD は system 指示に固定し、本文だけを毎回送る。
# temperature は 0。gemini-3.8-flash の思考は thinkingLevel low。
# XMLとして壊れているページと、XSDに合わないページはエラーを渡して1回だけやり直す。
#
# 環境変数:
#   GEMINI_API_KEY      AI Studio の API キー（必須）
#   GEMINI_MODEL        既定 gemini-3.8-flash
#   GEMINI_TEMPERATURE  既定 0
#   PAGES_PER_CALL      1回に渡すページ数（既定 4）
#   TIMEOUT_SEC         応答待ち秒数（既定 300）
#   MAX_RETRIES         空応答・タイムアウトのやり直し回数（既定 2）
#   MAX_CONTINUATIONS   出力が途中で切れたときに続きを求める回数（既定 3）
#   SLEEP_SEC           成功後の待機秒（既定 6）
#   INPUT_DIR           入力XMLディレクトリ
#   OUTPUT_DIR          manual XML の出力先

BASE=$(cd "$(dirname "$0")" && pwd)
LIB="$BASE/markup_manual_lib.py"
PROMPT="$BASE/prompt_markup.txt"
SCHEMA="$BASE/game_manual_schema.xsd"
INPUT_DIR=${INPUT_DIR:-$BASE/input/ndlocr-lite}
OUTPUT_DIR=${OUTPUT_DIR:-$BASE/output/annotated}
WORK_DIR=${WORK_DIR:-$(dirname "$OUTPUT_DIR")}
MANIFEST="$WORK_DIR/manifest.tsv"
JOB="$WORK_DIR/.job"
LOG="$WORK_DIR/markup.log"
LOCK="$WORK_DIR/.lock"
RAW="$WORK_DIR/raw"

PAGES_PER_CALL=${PAGES_PER_CALL:-4}
TIMEOUT_SEC=${TIMEOUT_SEC:-300}
MAX_RETRIES=${MAX_RETRIES:-2}
MAX_CONTINUATIONS=${MAX_CONTINUATIONS:-3}
SLEEP_SEC=${SLEEP_SEC:-6}
GEMINI_TEMPERATURE=${GEMINI_TEMPERATURE:-0}

ONLY=""
FROM_GAME=""
DRY_RUN=0
MAX_PAGES=""

usage() {
  sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --from)
      FROM_GAME=${2:-}
      if [ -z "$FROM_GAME" ]; then
        echo "--from にはゲームIDを指定してください" >&2
        exit 1
      fi
      shift 2
      ;;
    --max-pages)
      MAX_PAGES=${2:-}
      if ! printf '%s' "$MAX_PAGES" | grep -Eq '^[1-9][0-9]*$'; then
        echo "--max-pages には1以上の整数を指定してください" >&2
        exit 1
      fi
      shift 2
      ;;
    --pages-per-call)
      PAGES_PER_CALL=${2:-}
      if ! printf '%s' "$PAGES_PER_CALL" | grep -Eq '^[1-9][0-9]*$'; then
        echo "--pages-per-call には1以上の整数を指定してください" >&2
        exit 1
      fi
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "不明なオプションです: $1" >&2
      usage
      exit 1
      ;;
    *)
      if [ -n "$ONLY" ]; then
        echo "ゲームIDは1つだけ指定できます" >&2
        exit 1
      fi
      ONLY=$1
      shift
      ;;
  esac
done

if [ -n "$ONLY" ] && [ -n "$FROM_GAME" ]; then
  echo "ゲームIDの指定と --from は同時に使えません" >&2
  exit 1
fi

if [ ! -f "$PROMPT" ] || [ ! -f "$LIB" ]; then
  echo "prompt_markup.txt または markup_manual_lib.py がありません" >&2
  exit 1
fi

if [ ! -d "$INPUT_DIR" ]; then
  echo "入力ディレクトリがありません: $INPUT_DIR" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR" "$WORK_DIR" "$RAW"

log() {
  echo "$*" | tee -a "$LOG"
}

acquire_lock() {
  if mkdir "$LOCK" 2>/dev/null; then
    echo $$ > "$LOCK/pid"
    return 0
  fi
  old=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
    echo "別の markup_manual.sh が実行中です (pid $old)。終了してから再実行してください。" >&2
    exit 1
  fi
  echo $$ > "$LOCK/pid"
  log "前回のロックを引き継ぎました"
}

release_lock() {
  if [ -d "$LOCK" ]; then
    rm -f "$LOCK/pid"
    rmdir "$LOCK" 2>/dev/null || true
  fi
}

read_status() {
  GAME=""
  NEED_TITLE=0
  BLANK=0
  N_PAGES=0
  SEQS=""
  IS_LAST=0
  TOTAL_PAGES=0
  DONE_BEFORE=0
  LABEL=""
  while IFS='=' read -r key value; do
    case "$key" in
      game) GAME=$value ;;
      need_title) NEED_TITLE=$value ;;
      blank) BLANK=$value ;;
      n_pages) N_PAGES=$value ;;
      seqs) SEQS=$value ;;
      is_last) IS_LAST=$value ;;
      total_pages) TOTAL_PAGES=$value ;;
      done_before) DONE_BEFORE=$value ;;
      label) LABEL=$value ;;
    esac
  done < "$JOB/status"
}

run_gemini() {
  local req="$1"
  local out="$2"
  local err="$3"
  python3 "$LIB" generate \
    --system "$PROMPT" \
    --schema "$SCHEMA" \
    --user "$req" \
    --out "$out" \
    --err "$err" \
    --model "$GEMINI_MODEL" \
    --temperature "$GEMINI_TEMPERATURE" \
    --timeout "$TIMEOUT_SEC"
}

explain_api_failure() {
  python3 - "$1" << 'PY'
import json
import re
import sys
from pathlib import Path

err_path = Path(sys.argv[1])
err = err_path.read_text(errors="replace") if err_path.is_file() else ""
reports = re.findall(r"Full report available at:\s*(\S+)", err)

def unwrap(value, depth=0):
    if depth > 6 or not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("{") and not text.startswith('"'):
        return text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, str):
        return unwrap(parsed, depth + 1)
    if isinstance(parsed, dict):
        inner = parsed.get("error", parsed)
        if isinstance(inner, dict) and "message" in inner:
            code = inner.get("code", "")
            status = inner.get("status", "")
            deeper = unwrap(inner.get("message", ""), depth + 1)
            if deeper and deeper != inner.get("message", ""):
                return f"{code} {status} {deeper}".strip()
            return f"{code} {status} {inner.get('message', '')}".strip()
    return None

messages = []
for report in reports:
    path = Path(report)
    if not path.is_file():
        continue
    try:
        data = json.loads(path.read_text(errors="replace"))
    except json.JSONDecodeError:
        continue
    message = unwrap(data.get("error", {}).get("message", ""))
    if message:
        messages.append(message)
if messages:
    print(messages[0][:500])
PY
}

collect_response() {
  local out="$1"
  local err="$2"
  local work="$3"
  local dest="$JOB/response.txt"
  cp "$out" "$dest"
  if grep -q '<page' "$dest"; then
    return 0
  fi
  if grep -q '<page' "$err"; then
    cat "$err" >> "$dest"
  fi
  if grep -q '<page' "$dest"; then
    return 0
  fi
  find "$work" -type f -print | while IFS= read -r found; do
    case "$found" in
      "$out"|"$err") continue ;;
    esac
    if grep -q '<page' "$found"; then
      cat "$found" >> "$dest"
    fi
  done
  if grep -q '<page' "$dest"; then
    return 0
  fi
  return 1
}

build_request() {
  local mode="$1"
  local attempt="$2"
  {
    echo "===== この呼び出し ====="
    echo "ゲームID: $GAME"
    echo "入力ページ数: $N_PAGES"
    if [ "$mode" = "continue" ]; then
      echo "出力が </page> の前で切れています。すでに書いた部分は繰り返さず、続きだけを出力してください。"
      echo "===== 入力（参照。最初から書き直さない） ====="
      cat "$JOB/input.txt"
      echo "===== 直前の出力の末尾 ====="
      tail -c 6000 "$JOB/partial_raw.txt"
      echo
    else
      if [ "$NEED_TITLE" = "1" ]; then
        echo "これはこのマニュアルの最初のバッチです。XMLより前の1行目を TITLE: にしてください。題名が原文から取れなければ TITLE: $GAME としてください。"
      else
        echo "これは続きのページです。TITLE 行は書かず、page 要素だけを出力してください。"
      fi
      if [ "$attempt" -gt 0 ] && [ -s "$JOB/last_error.txt" ]; then
        echo "===== 前回の出力は検証エラー ====="
        echo "次のエラーだけを直し、page 要素を最初から書き直してください。地の文の文字は変えないでください。"
        echo "expected にあって got に無い文字は、OCRの誤りに見えても必ず戻す。行を前後へ吸収したり、表のマスの間に空白を足したりしない。"
        cat "$JOB/last_error.txt"
        echo
        if [ -s "$JOB/response.txt" ]; then
          echo "===== 前回の出力 ====="
          cat "$JOB/response.txt"
          echo
        fi
      fi
      echo "===== 入力 ====="
      cat "$JOB/input.txt"
    fi
  } > "$JOB/request.txt"
}

save_failure() {
  local dir="$RAW/$GAME"
  mkdir -p "$dir"
  if [ -f "$JOB/response.txt" ]; then
    cp "$JOB/response.txt" "$dir/seq${SEQS}.fail.txt"
  fi
  if [ -f "$JOB/last_error.txt" ]; then
    cp "$JOB/last_error.txt" "$dir/seq${SEQS}.error.txt"
  fi
  if [ -f "$JOB/gemini.err" ]; then
    cp "$JOB/gemini.err" "$dir/seq${SEQS}.stderr.txt"
  fi
}

process_job() {
  local mode attempt cont success gemini_status accept_status
  if [ "$BLANK" = "1" ]; then
    python3 "$LIB" accept-blank --job-dir "$JOB" --output-dir "$OUTPUT_DIR" --schema "$SCHEMA" --model "$GEMINI_MODEL" > "$JOB/accept_msg.txt"
    accept_status=$?
    cat "$JOB/accept_msg.txt"
    if [ "$accept_status" -ne 0 ]; then
      log "✗ 空ページを書き出せませんでした: $GAME seq=$SEQS"
      save_failure
      return 1
    fi
    return 0
  fi

  mode="fresh"
  attempt=0
  repairs=0
  cont=0
  rate_waits=0
  success=0
  while [ "$success" -eq 0 ]; do
    if [ "$mode" = "fresh" ] && [ "$attempt" -gt "$MAX_RETRIES" ]; then
      break
    fi
    if [ "$mode" = "continue" ] && [ "$cont" -gt "$MAX_CONTINUATIONS" ]; then
      log "続きの取得回数の上限に達したため、このページを最初からやり直します"
      mode="fresh"
      cont=0
      attempt=$((attempt + 1))
      rm -f "$JOB/partial_raw.txt"
      continue
    fi

    build_request "$mode" "$attempt"
    run_gemini "$JOB/request.txt" "$JOB/gemini.out" "$JOB/gemini.err"
    gemini_status=$?
    cp "$JOB/gemini.out" "$JOB/response.txt"
    usage_line=$(grep '^USAGE ' "$JOB/gemini.err" 2>/dev/null | tail -1 || true)
    if [ -n "$usage_line" ]; then
      log "$usage_line"
    fi
    api_line=$(grep '^API_ERROR' "$JOB/gemini.err" 2>/dev/null | head -1 || true)
    if [ -n "$api_line" ]; then
      log "✗ $api_line"
      if printf '%s' "$api_line" | grep -Eq "402|prepayment|credits are depleted|Payment Required"; then
        log "この API キーの前払いクレジットが残っていません。AI Studio のプロジェクトで枠を確認してから、同じコマンドを再実行してください。"
        save_failure
        return 1
      fi
      if printf '%s' "$api_line" | grep -Eq "no longer available|NOT_FOUND"; then
        log "指定モデルをこの API キーでは使えません。GEMINI_MODEL を変えてから、同じコマンドを再実行してください。"
        save_failure
        return 1
      fi
      if printf '%s' "$api_line" | grep -Eq "API key not valid|API_KEY_INVALID"; then
        log "GEMINI_API_KEY が無効です。AI Studio で作ったキーを export し直してから、同じコマンドを再実行してください。"
        save_failure
        return 1
      fi
    fi
    if grep -q "CERTIFICATE_VERIFY_FAILED\|SSL_ERROR" "$JOB/gemini.err" 2>/dev/null; then
      log "✗ HTTPS の証明書を検証できず、API に接続できませんでした。クレジット不足ではありません。"
      save_failure
      return 1
    fi

    if ! grep -q '<page' "$JOB/response.txt" 2>/dev/null; then
      api_msg=$(explain_api_failure "$JOB/gemini.err")
      if [ -n "$api_msg" ]; then
        log "✗ Gemini API: $api_msg"
        if printf '%s' "$api_msg" | grep -Eq "402|prepayment|credits are depleted|Payment Required"; then
          log "この API キーの前払いクレジットが残っていません。AI Studio のプロジェクトで枠を確認してから、同じコマンドを再実行してください。"
          save_failure
          return 1
        fi
      fi
    fi

    if [ "$gemini_status" -eq 124 ] || [ ! -s "$JOB/response.txt" ]; then
      err_line=$(grep -E "Error:|TIMEOUT" "$JOB/gemini.err" 2>/dev/null | head -1 || true)
      log "✗ Geminiの応答が無い、またはタイムアウトしました: $GAME seq=$SEQS (exit=$gemini_status) ${err_line}"
      if grep -Eq "no longer available|NOT_FOUND" "$JOB/gemini.err" 2>/dev/null; then
        log "指定モデルをこの API キーでは使えません。GEMINI_MODEL を変えてから、同じコマンドを再実行してください。"
        save_failure
        return 1
      fi
      if grep -Eq "API key not valid|API_KEY_INVALID|unauthenticated|GEMINI_API_KEY environment variable not found" "$JOB/gemini.err" 2>/dev/null; then
        log "GEMINI_API_KEY が無効です。AI Studio で作ったキーを export し直してから、同じコマンドを再実行してください。"
        save_failure
        return 1
      fi
      if grep -Eq "429|RESOURCE_EXHAUSTED|Too Many Requests|quota" "$JOB/gemini.err" 2>/dev/null; then
        rate_waits=$((rate_waits + 1))
        if [ "$rate_waits" -gt 10 ]; then
          log "無料枠のレート制限が続いたため停止しました。時間をおいて同じコマンドで再開してください。"
          save_failure
          return 1
        fi
        log "無料枠のレート制限です。30秒待って再試行します ($rate_waits/10)"
        sleep 30
        continue
      fi
      if [ "$mode" = "continue" ]; then
        cont=$((cont + 1))
      else
        attempt=$((attempt + 1))
      fi
      continue
    fi

    if [ "$mode" = "continue" ]; then
      python3 "$LIB" accept --job-dir "$JOB" --output-dir "$OUTPUT_DIR" --response "$JOB/response.txt" --schema "$SCHEMA" --model "$GEMINI_MODEL" --continue-output > "$JOB/accept_msg.txt"
    else
      python3 "$LIB" accept --job-dir "$JOB" --output-dir "$OUTPUT_DIR" --response "$JOB/response.txt" --schema "$SCHEMA" --model "$GEMINI_MODEL" > "$JOB/accept_msg.txt"
    fi
    accept_status=$?
    cat "$JOB/accept_msg.txt" | tee -a "$LOG" >/dev/null
    cat "$JOB/accept_msg.txt"

    if [ "$accept_status" -eq 0 ]; then
      success=1
    elif [ "$accept_status" -eq 4 ]; then
      mode="continue"
      cont=$((cont + 1))
      log "応答が途中で切れたため、続きを要求します ($cont/$MAX_CONTINUATIONS)"
    else
      repairs=$((repairs + 1))
      if [ "$repairs" -gt 1 ]; then
        log "検証エラーのやり直しも失敗したため停止します"
        save_failure
        return 1
      fi
      log "検証に失敗したため、エラーを渡して1回だけやり直します"
      mode="fresh"
      cont=0
      attempt=1
      rm -f "$JOB/partial_raw.txt"
    fi
  done

  if [ "$success" -ne 1 ]; then
    save_failure
    return 1
  fi
  return 0
}

python3 "$LIB" manifest --input "$INPUT_DIR" --out "$MANIFEST" || exit 1

SUMMARY=$(python3 "$LIB" summary --manifest "$MANIFEST" --output-dir "$OUTPUT_DIR" --only "$ONLY" --from-game "$FROM_GAME") || exit 1
echo "$SUMMARY"
DONE_AT_START=$(echo "$SUMMARY" | awk '/^games=/{for(i=1;i<=NF;i++){split($i,a,"="); if(a[1]=="done") print a[2]}}')
TOTAL_PAGES_ALL=$(echo "$SUMMARY" | awk '/^games=/{for(i=1;i<=NF;i++){split($i,a,"="); if(a[1]=="pages") print a[2]}}')

if [ "$DRY_RUN" -eq 1 ]; then
  exit 0
fi

if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "GEMINI_API_KEY がありません。Google AI Studio の API キーを設定してから再実行してください。" >&2
  echo "  export GEMINI_API_KEY=\"取得したキー\"" >&2
  exit 1
fi

if [ -z "${GEMINI_MODEL:-}" ]; then
  GEMINI_MODEL=gemini-3.8-flash
fi

acquire_lock
trap 'release_lock' EXIT INT TERM

processed_this_run=0
log "Start $(date "+%Y-%m-%d %H:%M:%S") api=gemini-api-key model=$GEMINI_MODEL temperature=$GEMINI_TEMPERATURE pages_per_call=$PAGES_PER_CALL done=$DONE_AT_START/$TOTAL_PAGES_ALL only=${ONLY:-all} from=${FROM_GAME:-begin}"

while true; do
  if [ -n "$MAX_PAGES" ] && [ "$processed_this_run" -ge "$MAX_PAGES" ]; then
    log "指定したページ数 ($MAX_PAGES) まで処理しました。同じコマンドで続きから再開できます。"
    exit 0
  fi

  call_pages=$PAGES_PER_CALL
  if [ -n "$MAX_PAGES" ]; then
    remain=$((MAX_PAGES - processed_this_run))
    if [ "$call_pages" -gt "$remain" ]; then
      call_pages=$remain
    fi
  fi

  python3 "$LIB" next \
    --manifest "$MANIFEST" \
    --output-dir "$OUTPUT_DIR" \
    --job-dir "$JOB" \
    --only "$ONLY" \
    --from-game "$FROM_GAME" \
    --pages-per-call "$call_pages" > "$WORK_DIR/next.msg"
  next_status=$?
  if [ "$next_status" -eq 2 ]; then
    log "処理が完了しています。結果: $OUTPUT_DIR"
    exit 0
  fi
  if [ "$next_status" -ne 0 ]; then
    log "✗ 次のページを決められませんでした"
    exit 1
  fi
  read_status
  log "Processing $GAME seq=$SEQS ($((DONE_BEFORE + 1))/$TOTAL_PAGES) $LABEL ... ($(date "+%H:%M:%S"))"

  if ! process_job; then
    now=$((DONE_AT_START + processed_this_run))
    log "✗ Error processing $GAME seq=$SEQS"
    log "完了済みは $now/$TOTAL_PAGES_ALL ページです。未完了のページは書き込んでいません。"
    log "失敗時の応答: $RAW/$GAME/"
    log "同じコマンドを再実行すると、このページから再開します。"
    exit 1
  fi

  processed_this_run=$((processed_this_run + N_PAGES))
  now=$((DONE_AT_START + processed_this_run))
  if [ "$TOTAL_PAGES_ALL" -gt 0 ]; then
    percent=$((now * 100 / TOTAL_PAGES_ALL))
  else
    percent=100
  fi
  log "✓ Processed $GAME seq=$SEQS"
  log "Progress: $now/$TOTAL_PAGES_ALL ($percent%)"
  sleep "$SLEEP_SEC"
done
