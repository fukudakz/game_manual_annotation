# ゲームマニュアルのOCRとLLMコーディング フォルダ

作成：2026-10-16

Google ドライブの共有フォルダ `共有：ゲームアーカイブコーパスプロジェクト/game_manual` の内容です。立命館ゲーム研究センター（RCGS）所蔵のゲーム取扱説明書を、ページ画像、OCR、構造化 XML として置いています。

このリポジトリの `markup_manual.sh`、`markup_manual_lib.py`、`prompt_markup.txt`、`game_manual_schema.xsd` は、フォルダ内の `src/` とスキーマの作業コピーです。

## フォルダ構成

| パス | 内容 |
| --- | --- |
| `game_manual_schema.xsd` | マークアップ XML のスキーマ |
| `ndlocr-lite-20261008T063529Z-1-001.zip` | NDLOCR-Lite の結果一式（約 44 MB、2026-10-08） |
| `ndlocr-lite_results/` | 展開済みの OCR 結果。17,409 ファイル、486 ゲーム |
| `webp/` | ページ画像。5,706 ファイル、486 ゲーム |
| `xml_annotated_sample/` | スキーマに沿ってマークアップしたサンプル。37 ファイル |
| `src/` | OCR XML をマニュアル XML に変換するスクリプトとプロンプト |

資料を識別するID は `RCGSa` と数字 7 桁です。画像と OCR は `RCGSa0000002` から `RCGSa0023418` まで、欠番を含めて 486 ゲームです。1 ゲームが 1 冊のマニュアルに対応します。ファイル名は `ゲームID_冊子コード_ページ番号` です。冊子コードは `m001` のような冊子や `p001` のような付属資料、ページ番号は 3 桁です。同じページに複数の XML があるときは `_converted.xml` を使います。

## `webp/`

ページごとの WebP 画像です。ファイル名の例は `RCGSa0000005_m001_001_converted.webp` です。

## `ndlocr-lite_results/`

NDLOCR-Lite のページ単位の出力です。1 ページにつき XML、JSON、テキストが並びます。

| 拡張子 | ファイル数 |
| --- | --- |
| `.txt` | 5,804 |
| `.json` | 5,803 |
| `.xml` | 5,802 |

XML のルートは `OCRDATASET` です。`PAGE` の `IMAGENAME` が対応する WebP を指し、`LINE` の `TYPE` と `STRING` が行種別と本文です。行種別には「本文」「タイトル本文」「キャプション」「広告文字」「割注」などがあります。座標は `X`、`Y`、`WIDTH`、`HEIGHT` と `POLYGON` に入っています。マークアップ処理は座標を送らず、`TYPE` と `STRING` だけを使います。

## `xml_annotated_sample/`

人手またはモデルが `game_manual_schema.xsd` に沿って構造化したマニュアルです。

- `CLV-P-*_labeled.xml` が 30 件。ファミコンなどの旧サンプルで、コメントに `Created by claude-4-sonnet` とある。ルートの識別子は `game_id`。
- `RCGSa0000002.xml` から `RCGSa0000008.xml` までが 7 件。現行の出力形式で、`identifier` と `model` を持つ。

現行の 1 件は次の形です。

```xml
<manual title="RCGSa0000005" lang="ja" identifier="RCGSa0000005" model="gemini-3.8-flash">
  <page number="1" img="RCGSa0000005_m001_001_converted.webp">
    <section type="cover">
      <head>スーパーファミコン</head>
      <p>SUPER Famicom</p>
    </section>
  </page>
</manual>
```

`page` の中は `section` です。`section` は `head`、`p`、`list`、`credit`、`copyRight`、入れ子の `section` を持ちます。`type` は `cover`、`preface`、`toc`、`control`、`gameplay`、`legal` などの列挙値です。本文中の固有表現は `character`、`device`、`partOfDevice`、`person`、`org`、`location`、`time` などで包みます。

## `game_manual_schema.xsd`

`manual` をルートとするスキーマです。必須属性は `identifier`（資料の管理 ID）と `model`（エンコーディングに使ったモデル名）です。`title` と `lang` は任意です。`manual` は 1 つ以上の `page` を持ち、`page` は `number` を必須とします。

## `src/`

| ファイル | 役割 |
| --- | --- |
| `markup_manual.sh` | ゲーム単位で OCR XML を読み、Gemini にマークアップさせ、検証して保存する |
| `markup_manual_lib.py` | マニフェスト作成、ページ選択、応答の検証、XSD 検査 |
| `prompt_markup.txt` | モデルへ渡すマークアップ規則 |
| `prompt_old.txt` | 以前のプロンプト |

`RCGSa` と数字 7 桁が 1 つの `manual` です。ページは冊子コード、その中のページ番号の順に並びます。地のテキストは、OCR の改行を文の途中でつなぐ以外は変えません。見出しが無い区間には `head` を付けません。空応答、タイムアウト、途中で切れた応答のときは、完了済みの `page` だけを残して止まります。同じコマンドを再実行すると続きから処理します。

```bash
./markup_manual.sh
./markup_manual.sh RCGSa0000002
./markup_manual.sh --from RCGSa0000100
./markup_manual.sh --pages-per-call 4 --max-pages 8
./markup_manual.sh --dry-run
```

認証は Google AI Studio の API キー（`GEMINI_API_KEY`）です。規則と XSD は system 指示に固定し、本文だけを毎回送ります。temperature は 0 です。既定モデルは `gemini-3.8-flash` で、思考レベルは low です。XML として壊れているページと、XSD に合わないページは、エラーを渡して 1 回だけやり直します。

| 環境変数 | 既定 | 意味 |
| --- | --- | --- |
| `GEMINI_API_KEY` | なし（必須） | AI Studio の API キー |
| `GEMINI_MODEL` | `gemini-3.8-flash` | 使用モデル |
| `GEMINI_TEMPERATURE` | `0` | 生成温度 |
| `PAGES_PER_CALL` | `4` | 1 回に渡すページ数 |
| `TIMEOUT_SEC` | `300` | 応答待ち秒数 |
| `MAX_RETRIES` | `2` | 空応答・タイムアウトのやり直し回数 |
| `MAX_CONTINUATIONS` | `3` | 出力が途中で切れたときに続きを求める回数 |
| `SLEEP_SEC` | `6` | 成功後の待機秒 |
| `INPUT_DIR` | `input/ndlocr-lite` | 入力 XML ディレクトリ |
| `OUTPUT_DIR` | `output/annotated` | manual XML の出力先 |
