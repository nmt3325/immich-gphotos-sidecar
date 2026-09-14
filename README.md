# immich-gphotos-sidecar

Immich の**コンテンツ（オリジナルファイル）**・**サイドカー情報（元ファイル名やメタデータ）**・**アルバム**を
Google フォトへバックアップするサイドカー用 Docker コンテナです。

- アップロードは [xob0t/gotohp](https://github.com/xob0t/gotohp) の CLI（Google フォト非公式 API）
- アルバム作成・追加は gotohp の `-a/--album`（既定）または Google Photos Library API（任意）
- メタデータは **EXIF/XMP への埋め込み**（既定）と **ローカルのサイドカー JSON / XMP**

```
  Immich REST API                この コンテナ                        Google フォト
 ┌───────────────┐   assets   ┌──────────────────────────┐  gotohp  ┌──────────────┐
 │ /search/metadata ├──────────►│ 1. 差分検出 (sqlite)      │─────────►│ メディア      │
 │ /assets/{id}     │  originals│ 2. サイドカー JSON/XMP     │  -a      │ アルバム      │
 │ /assets/{id}/original ───────►│ 3. EXIF/XMP 埋め込み      │─────────►│              │
 │ /albums          │  albums   │ 4. アルバム毎にステージング │          └──────────────┘
 │ /tags /people    ├──────────►│ 5. gotohp upload          │  Library API（任意）
 └───────────────┘            └──────────────────────────┘─────────► description / album
```

## バックアップされるもの

| 種類 | 保存先 | 内容 |
| --- | --- | --- |
| オリジナルファイル | Google フォト | `/api/assets/{id}/original` をそのままアップロード（元ファイル名を維持） |
| サイドカー情報 | `/sidecar/assets/<xx>/<assetId>.json` と `.xmp` | 元ファイル名・元パス・チェックサム・撮影日時・EXIF・GPS・説明・タグ・人物・お気に入り・アーカイブ・所属アルバム |
| 埋め込みメタデータ | Google フォト側のファイル本体 | 撮影日時・GPS・説明・キーワード・レーティング・`XMP-dc:Identifier=immich:<assetId>`（exiftool） |
| アルバム | Google フォト + `/sidecar/library/albums.json` | Immich のアルバム名でアルバムを作成し、対応するメディアを追加 |
| ライブラリ全体 | `/sidecar/library/{albums,tags,people}.json`, `manifest.jsonl` | 復元・照合用のスナップショット（assetId ↔ MediaKey 対応表を含む） |

## クイックスタート

```bash
cp docker-compose.example.yml docker-compose.yml
cp .env.example .env
$EDITOR .env            # IMMICH_BASE_URL / IMMICH_API_KEY / GOTOHP_AUTH_STRING

docker compose pull                       # ghcr.io からイメージ取得（自前ビルドなら docker compose build）
docker compose run --rm immich-gphotos-sidecar doctor   # 事前チェック
docker compose run --rm immich-gphotos-sidecar --max-assets 5 run   # 小さく試す
docker compose up -d                      # 常駐（既定 1 日 1 回）
docker compose logs -f
```

### Google アカウントの認証（Auth String）

gotohp は Google フォト Android アプリの Auth String を使います。取得方法は 2 通り。

1. **`GOTOHP_AUTH_STRING` に入れる** — 起動時に自動で `creds add` され、`/config/gotohp.config` に保存されます。
2. **後から手で登録する**
   ```bash
   docker compose exec immich-gphotos-sidecar gotohp creds add '<auth string>'
   docker compose exec immich-gphotos-sidecar gotohp creds list
   docker compose exec immich-gphotos-sidecar gotohp creds set you@gmail.com
   ```

Auth String は gotohp の README にある手順（Android の Google フォトアプリのトラフィックから
`Authorization: Bearer` ではなく mobile API 用の auth string、あるいは Embedded Setup の
`oauth_token` クッキー）で取得します。認証情報は `/config` に残るので、コンテナを作り直しても再入力は不要です。

### Google Photos Library API（任意）

`ALBUM_BACKEND=library_api` / `METADATA_BACKEND=library_api|both` を使う場合のみ必要です。

1. Google Cloud で OAuth クライアント（デスクトップ）を作成し、Photos Library API を有効化
2. スコープ `https://www.googleapis.com/auth/photoslibrary` でリフレッシュトークンを取得
3. `GPHOTOS_CLIENT_ID` / `GPHOTOS_CLIENT_SECRET` / `GPHOTOS_REFRESH_TOKEN` を `.env` に設定

> **重要な制約**: 2025-03-31 以降、Library API は「**その OAuth クライアント自身がアップロードしたメディア**」しか
> 参照・編集できません。gotohp は非公式のモバイル API でアップロードするため、Library API からは
> 別アプリの作成物として扱われ、説明文の書き換えやアルバム追加ができません。
> そのため既定値は `ALBUM_BACKEND=gotohp` / `METADATA_BACKEND=embed` です。
> Library API バックエンドは「Library API 経由でアップロードした資産を扱う」等の特殊用途向けです。

## イメージ（GHCR）と自動ビルド

`main` への push / `v*` タグ / 手動実行（workflow_dispatch）で GitHub Actions が
E2E テスト → `linux/amd64` + `linux/arm64` のビルド → GHCR への push を自動実行します。

```bash
docker pull ghcr.io/nmt3325/immich-gphotos-sidecar:latest
```

| タグ | 内容 |
| --- | --- |
| `latest` | `main` の最新ビルド |
| `main` | ブランチ名タグ（`latest` と同じ内容） |
| `sha-<短縮SHA>` | コミット単位で固定したいとき |
| `v1.2.3` / `1.2` | `v*` タグを push したとき |

リポジトリが private の間は GHCR のパッケージも private なので、pull 側で
`read:packages` 権限の PAT を使った `docker login ghcr.io` が必要です
（Package settings で public にすれば不要）。

## Immich の docker compose に同居させる

Immich の `docker-compose.yml` と同じフォルダに置く場合、**Immich の `.env` は触らず**、
サイドカー用の環境変数は `sidecar.env` という別名ファイルにして `env_file:` で読み込みます。

```bash
cd /path/to/immich            # immich の docker-compose.yml と .env がある場所
cp <this-repo>/docker-compose.immich.yml .
cp <this-repo>/.env.example sidecar.env
$EDITOR sidecar.env           # IMMICH_API_KEY / GOTOHP_AUTH_STRING など
docker compose -f docker-compose.yml -f docker-compose.immich.yml up -d
```

- `.env` は「compose ファイル内の `${...}` を埋める補間用」で、プロジェクトディレクトリに
  1 つだけ自動で読まれます。`env_file:` は「コンテナへ渡す環境変数」で任意のファイル名を
  指定でき、補間には使われません。だから `sidecar.env` は Immich の `.env` と衝突しません。
- Immich の `.env` への追記は非推奨です。`immich-server` は `env_file: .env` を読むので、
  `GOTOHP_AUTH_STRING` などの秘密が Immich のコンテナにも渡ってしまいます。
- Immich の `.env` の値を使いたいときだけ `environment:` 側で `${TZ:-Asia/Tokyo}` のように参照します。

`include:` / `COMPOSE_FILE` / 別プロジェクト + external network などの選択肢は
[docs/immich-compose.md](docs/immich-compose.md) にまとめています。

## コマンド

```bash
docker compose run --rm immich-gphotos-sidecar doctor    # 設定・接続・認証・exiftool の確認
docker compose run --rm immich-gphotos-sidecar run       # 1 回だけ実行（差分）
docker compose run --rm immich-gphotos-sidecar run --full-scan        # 透かし(watermark)を無視して全件照合
docker compose run --rm immich-gphotos-sidecar run --dry-run          # アップロードせずサイドカーだけ生成
docker compose run --rm immich-gphotos-sidecar run --max-assets 50    # 件数制限
docker compose run --rm immich-gphotos-sidecar stats     # 状態 DB の統計
docker compose run --rm immich-gphotos-sidecar gotohp upload /work/foo -r   # 生の gotohp
```

`run` は JSON のレポートを標準出力に出し、`/sidecar/reports/*.json` にも保存します。
終了コードは `0`=成功 / `1`=一部失敗 / `2`=設定不備。

## ボリューム

| パス | 用途 | 永続化 |
| --- | --- | --- |
| `/state` | `sidecar-state.sqlite3`（assetId ↔ MediaKey、アルバム対応、透かし） | **必須** |
| `/sidecar` | サイドカー JSON/XMP、ライブラリスナップショット、レポート | **必須** |
| `/config` | `gotohp.config`（Google 認証情報） | **必須** |
| `/work` | ダウンロードとステージングの作業領域（実行後に自動削除） | 任意（tmpfs 可） |

`/work` にはアップロード対象の一時コピーが置かれるため、`MAX_ASSETS_PER_RUN` と
`UPLOAD_BATCH_SIZE` に見合った空き容量（既定なら数 GB）を確保してください。

## 主な環境変数

完全な一覧と既定値は [.env.example](.env.example) を参照。

| 変数 | 既定 | 説明 |
| --- | --- | --- |
| `IMMICH_BASE_URL` / `IMMICH_API_KEY` | – | **必須**。Immich の URL と API キー |
| `GOTOHP_AUTH_STRING` | – | 初回に `creds add` する Auth String |
| `GOTOHP_ACCOUNT` | – | 複数アカウント時の選択（部分一致） |
| `GOTOHP_THREADS` | `3` | gotohp の並列アップロード数 |
| `GOTOHP_NO_TUI` | `true` | `--no-tui` を付ける（v0.10 以降）。古い版では自動で TUI+PTY にフォールバック |
| `GOTOHP_EXTRA_ARGS` | – | 例: `--pair-live-photos --date-from-filename --saver` |
| `ALBUM_BACKEND` | `gotohp` | `gotohp` / `library_api` / `none` |
| `ALBUM_NAME_TEMPLATE` | `{album}` | 例: `Immich / {album}` |
| `BACKFILL_ALBUMS` | `true` | 既にアップロード済みの資産にも後からアルバムを付け直す |
| `METADATA_BACKEND` | `embed` | `embed`（exiftool で埋め込み） / `library_api` / `both` / `none` |
| `ASSET_TYPES` | `IMAGE,VIDEO` | 対象の種類 |
| `INCLUDE_ARCHIVED` | `true` | Immich のアーカイブ済みも対象に含める |
| `FULL_SCAN` | `false` | 透かしを無視して毎回全件照合 |
| `MAX_ASSETS_PER_RUN` | `0`（無制限） | 1 回の実行で扱う上限 |
| `SCHEDULE_CRON` / `INTERVAL_MINUTES` | – / `1440` | cron 指定が優先。どちらも常駐モード用 |
| `DRY_RUN` | `false` | アップロードしない（サイドカー生成のみ） |

## 動作の詳細

1. **差分検出** — `POST /api/search/metadata` に `updatedAfter=<透かし − WATERMARK_SKEW_MINUTES>` を渡して
   変更分だけ取得。加えて失敗した資産（最大 5 回まで再試行）とアルバム未反映の資産を対象に加えます。
   透かしは「失敗 0 件で完走したとき」だけ進みます。
2. **サイドカー生成** — `sha256` で内容ハッシュを取り、変化が無ければ書き換えません（`exportedAt` は除外）。
3. **メタデータ埋め込み** — `exiftool` で撮影日時・GPS・説明・キーワード・`immich:<assetId>` を書き込みます。
   動画は `-api QuickTimeUTC=1` 経路。失敗しても致命的にはせず、警告のみでアップロードは継続します。
4. **アップロード** — アルバム単位（アルバム未所属は `_unsorted`）にハードリンクでステージングし、
   `gotohp upload <dir> -r -t N -a <album> -c /config/gotohp.config --no-tui` を実行。
   出力の ANSI/TUI ノイズを除去して末尾の JSON サマリを解析し、`MediaKey` を状態 DB に保存します。
   同名衝突は `名前_<assetId 先頭 8 桁>.ext` に退避します。
5. **冪等性** — 2 回目以降は「アップロード済み」「アルバム反映済み」の資産をスキップ。
   gotohp 側の重複排除が働いた場合も `MediaKey` が返るため、アルバム付与は正しく行われます。

## サイドカー JSON の例

```json
{
  "schemaVersion": 1,
  "assetId": "aaaa1111-…",
  "originalFileName": "IMG_0001.jpg",
  "originalPath": "upload/library/admin/2024/IMG_0001.jpg",
  "checksum": "c2hhMS1jaGVja3N1bS0x",
  "type": "IMAGE",
  "fileCreatedAt": "2024-05-01T10:00:00.000Z",
  "capturedAt": "2024-05-01T10:00:00.000Z",
  "isFavorite": true,
  "description": "Sakura at Ueno 上野の桜",
  "tags": ["travel", "spring"],
  "people": ["Kaichi"],
  "albums": ["2024 旅行"],
  "exifInfo": { "make": "Google", "model": "Pixel 8", "latitude": 35.7156, "longitude": 139.7745 }
}
```

`/sidecar/library/manifest.jsonl` には 1 行 1 資産で `assetId` / 元ファイル名 / `MediaKey` /
アルバム / チェックサムが並ぶので、Google フォト側との突き合わせや将来の復元に使えます。

## テスト

ネットワーク不要のエンドツーエンドテストが入っています（モック Immich + 偽 gotohp）。

```bash
python3 -m venv /tmp/venv && /tmp/venv/bin/pip install -r requirements.txt
bash tests/run_e2e.sh        # 3 回連続実行し、サイドカー・状態 DB・アルバム・冪等性を検証
```

## トラブルシューティング

| 症状 | 対処 |
| --- | --- |
| `doctor` の `gotohp_credentials` が NG | `GOTOHP_AUTH_STRING` を設定するか `gotohp creds add` を実行。`/config` が永続化されているか確認 |
| `exiftool` が NG | `METADATA_BACKEND=none` にするか、イメージを再ビルド（`libimage-exiftool-perl` が入ります） |
| アップロードが進まない/出力が壊れる | 古い gotohp なら `GOTOHP_NO_TUI=false` と `GOTOHP_USE_PTY=true` を試す |
| Library API が 403 | 上記の 2025-03-31 制約。`ALBUM_BACKEND=gotohp` / `METADATA_BACKEND=embed` に戻す |
| `/work` が膨らむ | `MAX_ASSETS_PER_RUN` と `UPLOAD_BATCH_SIZE` を下げる。`KEEP_LOCAL_COPIES=false` を維持 |
| 全部やり直したい | `/state/sidecar-state.sqlite3` を消す（gotohp 側の重複排除で二重アップロードは避けられます） |

## 制限事項

- gotohp は Google の**非公式**モバイル API を使います。仕様変更やアカウント制限のリスクは利用者が負います。
- Google フォトは EXIF/XMP の一部（タグ・人物・レーティング等）を UI に反映しません。原本の完全な情報は
  `/sidecar` のサイドカーファイル側に保持されます。
- 共有アルバムは既定で除外（`ALBUM_INCLUDE_SHARED=true` で対象化）。
- Immich 側の削除はミラーされません（Google フォト側は残ります）。
