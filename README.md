# immich-gphotos-sidecar

[![build](https://github.com/nmt3325/immich-gphotos-sidecar/actions/workflows/build.yml/badge.svg)](https://github.com/nmt3325/immich-gphotos-sidecar/actions/workflows/build.yml)

Immich の**コンテンツ（オリジナルファイル）**・**サイドカー情報（元ファイル名やメタデータ）**・**アルバム**を
Google フォトへバックアップするサイドカー用 Docker コンテナです。

- アップロードは [xob0t/gpmc](https://github.com/xob0t/gpmc) の Python ライブラリ（Google フォト非公式モバイル API）
- アルバム作成・追加は gpmc のアルバム API（既定）または Google Photos Library API（任意）
- メタデータは **EXIF/XMP への埋め込み**（既定）と **ローカルのサイドカー JSON / XMP**
- イメージは GitHub Actions が自動ビルドして GHCR へ push（`linux/amd64` + `linux/arm64`）

```
  Immich REST API                このコンテナ                         Google フォト
 ┌───────────────┐   assets   ┌──────────────────────────┐   gpmc   ┌──────────────┐
 │ /search/metadata ├──────────►│ 1. 差分検出 (sqlite)      │─────────►│ メディア      │
 │ /assets/{id}     │  originals│ 2. サイドカー JSON/XMP     │ album    │ アルバム      │
 │ /assets/{id}/original ───────►│ 3. EXIF/XMP 埋め込み      │─────────►│              │
 │ /albums          │  albums   │ 4. アルバム毎にステージング │          └──────────────┘
 │ /tags /people    ├──────────►│ 5. gpmc.Client.upload()   │  Library API（任意）
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
$EDITOR .env            # IMMICH_BASE_URL / IMMICH_API_KEY / GPMC_AUTH_DATA

docker compose pull                       # ghcr.io からイメージ取得（自前ビルドなら docker compose build）
docker compose run --rm immich-gphotos-sidecar doctor   # 事前チェック
docker compose run --rm immich-gphotos-sidecar --max-assets 5 run   # 小さく試す
docker compose up -d                      # 常駐（既定 1 日 1 回）
docker compose logs -f
```

`docker-compose.example.yml` は GHCR のイメージを使う単独運用向けです。既存の Immich スタックへ
相乗りさせる場合は [Immich の docker compose に同居させる](#immich-の-docker-compose-に同居させる)
（`.env` の衝突を避けて `sidecar.env` を使う手順）を参照してください。

### Google アカウントの認証（Auth Data）

gpmc は Google フォト Android アプリの auth data（旧 gotohp の Auth String と同じ文字列）を使います。
`Email=` と `Token=aas_et/...` を含む長いクエリ文字列が auth data であり、
**`oauth2_4/...` の `oauth_token` クッキーそのものは auth data ではありません**。

#### 方法 A: ブラウザのログインから作る（推奨）

gotohp と同じ交換処理をサイドカーに移植してあります。Android 端末も root も
パケットキャプチャも不要です。

1. ブラウザのシークレットウィンドウで <https://accounts.google.com/EmbeddedSetup> を開き、対象アカウントでログイン
2. DevTools → Application / Storage → Cookies → `https://accounts.google.com` → `oauth_token` の値をコピー
3. サイドカーに渡す（`-` は標準入力から読むので、シェル履歴に残りません）

```bash
# 対話的に貼り付ける
docker compose run --rm immich-gphotos-sidecar creds add -

# 変数経由で渡す（read -rs で入力すれば履歴にも残りません）
read -rs OAUTH_TOKEN
printf '%s' "$OAUTH_TOKEN" | docker compose run --rm -T immich-gphotos-sidecar creds add -
```

サイドカーは Play サービスの「アカウント追加」リクエストを再現して `oauth_token` を
マスタートークン（`aas_et/...`）に交換し、Google フォト用の auth data を組み立て、
gpmc で疎通確認したうえで `GPMC_AUTH_DATA_FILE`（既定 `/config/auth_data`、パーミッション 0600）に
保存します。`GPMC_AUTH_DATA` が空ならこのファイルが自動的に使われるので、`sidecar.env` の
編集は不要です（保存先の `/config` を永続化しておいてください）。

| コマンド | 用途 |
| --- | --- |
| `creds add <oauth_token>` / `creds add -` | oauth_token を auth data に交換して保存 |
| `creds add - --no-save` | 保存せず auth data を表示（`GPMC_AUTH_DATA` に貼る用） |
| `creds show` | どの auth data / アカウントが使われるか（トークンは伏字） |
| `creds test` | 現在の auth data で Google フォトに接続できるか確認 |

> `oauth_token` は短命かつ 1 回限りです。`BadAuthentication` になったら取り直してください。
> 交換後のマスタートークンはアカウント全体を操作できる強い資格情報なので、
> `/config/auth_data` や `sidecar.env` は `chmod 600` などで保護してください。

#### 方法 B: すでに auth data を持っている場合

`GPMC_AUTH_DATA` に貼るだけです（gpmc 自身の変数名 `GP_AUTH_DATA`、旧 `GOTOHP_AUTH_STRING` も
後方互換で読みます。[gotohp からの移行](#gotohp-からの移行) 参照）。
Android アプリのトラフィックから取得する手順は gpmc の README にあります。
生の gpmc CLI にも同じ値が渡るので、単体での動作確認もできます。

```bash
docker compose run --rm immich-gphotos-sidecar doctor       # 認証と gpmc の確認
docker compose run --rm immich-gphotos-sidecar creds show   # 使用中の auth data（伏字）
docker compose run --rm immich-gphotos-sidecar gpmc --help  # 生の gpmc CLI
```

`GPMC_CACHE_DIR`（既定 `/config`）に gpmc のハッシュキャッシュ（`~/.gpmc`）が残るので、
コンテナを作り直しても再アップロードは発生しません。

### gotohp からの移行

アップロードは gotohp（Go の CLI バイナリ）から [gpmc](https://github.com/xob0t/gpmc)（Python ライブラリ）に
置き換わりました。サブプロセス・pty・TUI 出力の解析は無くなり、`gpmc.Client` をサイドカーの
プロセス内で直接呼びます。

| 旧（gotohp） | 新（gpmc） |
| --- | --- |
| `GOTOHP_AUTH_STRING` | `GPMC_AUTH_DATA`（`GP_AUTH_DATA` も可。旧名も後方互換で読みます） |
| `GOTOHP_THREADS` | `GPMC_THREADS`（旧名も後方互換で読みます） |
| `ALBUM_BACKEND=gotohp` | `ALBUM_BACKEND=gpmc`（旧値は自動的に `gpmc` と解釈します） |
| `GOTOHP_BIN` / `GOTOHP_CONFIG` / `GOTOHP_ACCOUNT` / `GOTOHP_EXTRA_ARGS` / `GOTOHP_USE_PTY` / `GOTOHP_NO_TUI` / `GOTOHP_TIMEOUT` | 廃止（バイナリ配置は不要。gotohp の `creds add` はサイドカー内蔵の `creds add` に置き換え） |
| gotohp の設定/キャッシュ | `/config` 配下の `~/.gpmc`（`GPMC_CACHE_DIR`） |

- 状態 DB（`/state/sidecar-state.sqlite3`）はそのまま使えます。アップロード済み判定は MediaKey ベースのままです。
- ただし gpmc のアルバム API は「名前で既存アルバムを再利用」しないため、gotohp 時代に作られたアルバムは
  album key を持っていません。移行後の初回実行で同名アルバムが新規作成され、以降はその key を状態 DB に
  保存して追記します（重複が困る場合は Google フォト側で手動統合してください）。

### Google Photos Library API（任意）

`ALBUM_BACKEND=library_api` / `METADATA_BACKEND=library_api|both` を使う場合のみ必要です。

1. Google Cloud で OAuth クライアント（デスクトップ）を作成し、Photos Library API を有効化
2. リフレッシュトークンを取得（2025-03-31 以降に使えるスコープは `photoslibrary.appendonly` /
   `photoslibrary.edit.appcreateddata` / `photoslibrary.readonly.appcreateddata` の 3 つだけで、
   旧 `photoslibrary` / `photoslibrary.readonly` / `photoslibrary.sharing` は廃止済み）
3. `GPHOTOS_CLIENT_ID` / `GPHOTOS_CLIENT_SECRET` / `GPHOTOS_REFRESH_TOKEN` を設定（単独運用なら `.env`、Immich 同居なら `sidecar.env`）

> **重要な制約**: 2025-03-31 以降、Library API は「**その OAuth クライアント自身がアップロードしたメディア**」しか
> 参照・編集できません。gpmc は非公式のモバイル API でアップロードするため、Library API からは
> 別アプリの作成物として扱われ、説明文の書き換えやアルバム追加ができません。
> そのため既定値は `ALBUM_BACKEND=gpmc` / `METADATA_BACKEND=embed` です。
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
（リポジトリ → Packages → Package settings → Change visibility で public にすれば不要）。

```bash
echo '<read:packages の PAT>' | docker login ghcr.io -u <github ユーザー名> --password-stdin
```

更新は `docker compose pull && docker compose up -d`。特定のビルドに固定したいときは compose の
`image:` を `ghcr.io/nmt3325/immich-gphotos-sidecar:sha-<短縮SHA>` や `:v1.2.3` に書き換えます。

ワークフローは [.github/workflows/build.yml](.github/workflows/build.yml) の 2 ジョブ構成です。

| ジョブ | 内容 |
| --- | --- |
| `test`（表示名 `e2e (mock immich + fake gpmc)`） | `libimage-exiftool-perl` を入れて `PY=python bash tests/run_e2e.sh`（モック Immich + 偽の `gpmc` パッケージの E2E） |
| `image`（表示名 `image (amd64 + arm64)`） | `test` 成功後に amd64 をビルドし、導入された `gpmc` のバージョン・`gpmc --help`・`version` でスモークテスト → amd64 + arm64 を GHCR へ push |

- Pull Request では `test` と amd64 のビルド・スモークテストだけを行い、GHCR には push しません。
- アップローダは pip 依存（`gpmc`）なので、ビルド引数でなく `requirements.txt` の `gpmc>=0.9,<1` を書き換えて固定します。
- リリースは `git tag v1.2.3 && git push origin v1.2.3`。
- キャッシュは GitHub Actions cache（`type=gha`）。arm64 は QEMU エミュレーションなので初回は数分かかります。

## Immich の docker compose に同居させる

Immich の `docker-compose.yml` と同じフォルダに置く場合、**Immich の `.env` は触らず**、
サイドカー用の環境変数は `sidecar.env` という別名ファイルにして `env_file:` で読み込みます。

```bash
cd /path/to/immich            # immich の docker-compose.yml と .env がある場所
cp <this-repo>/docker-compose.immich.yml .
cp <this-repo>/.env.example sidecar.env
$EDITOR sidecar.env           # IMMICH_API_KEY / GPMC_AUTH_DATA など
docker compose -f docker-compose.yml -f docker-compose.immich.yml up -d
```

```
/path/to/immich/
├── docker-compose.yml          # Immich 公式（そのまま）
├── docker-compose.immich.yml   # ← このリポジトリの overlay
├── .env                        # Immich 用（そのまま）
└── sidecar.env                 # ← サイドカー用
```

- `.env` は「compose ファイル内の `${...}` を埋める補間用」で、プロジェクトディレクトリに
  1 つだけ自動で読まれます。`env_file:` は「コンテナへ渡す環境変数」で任意のファイル名を
  指定でき、補間には使われません。だから `sidecar.env` は Immich の `.env` と衝突しません。
- Immich の `.env` への追記は非推奨です。`immich-server` と `immich-machine-learning` は
  `env_file: .env` を読むので、`GPMC_AUTH_DATA` などの秘密が Immich のコンテナにも渡ってしまいます。
- Immich の `.env` の値を使いたいときだけ `environment:` 側で `${TZ:-Asia/Tokyo}` のように参照します。
- overlay の `IMMICH_BASE_URL` は既定で `http://immich-server:2283`（同じ compose ネットワーク内）です。

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
docker compose run --rm immich-gphotos-sidecar creds add -           # oauth_token → auth data（標準入力から）
docker compose run --rm immich-gphotos-sidecar creds show            # 使用中の auth data（トークンは伏字）
docker compose run --rm immich-gphotos-sidecar creds test            # 認証だけを確認
docker compose run --rm immich-gphotos-sidecar gpmc /work/foo --recursive --threads 3   # 生の gpmc CLI
```

Immich と同居させている場合は各コマンドの前に `-f docker-compose.yml -f docker-compose.immich.yml`
（または `COMPOSE_FILE` の設定）を付けてください。

`run` は JSON のレポートを標準出力に出し、`/sidecar/reports/*.json` にも保存します。
終了コードは `0`=成功 / `1`=一部失敗 / `2`=設定不備。

## ボリューム

| パス | 用途 | 永続化 |
| --- | --- | --- |
| `/state` | `sidecar-state.sqlite3`（assetId ↔ MediaKey、アルバム対応、透かし） | **必須** |
| `/sidecar` | サイドカー JSON/XMP、ライブラリスナップショット、レポート | **必須** |
| `/config` | gpmc のハッシュキャッシュ（`~/.gpmc`、`GPMC_CACHE_DIR`） | **必須** |
| `/work` | ダウンロードとステージングの作業領域（実行後に自動削除） | 任意（tmpfs 可） |

`/work` にはアップロード対象の一時コピーが置かれるため、`MAX_ASSETS_PER_RUN` と
`UPLOAD_BATCH_SIZE` に見合った空き容量（既定なら数 GB）を確保してください。

## 主な環境変数

完全な一覧と既定値は [.env.example](.env.example) を参照（Immich 同居時は同じ内容を `sidecar.env` に置きます）。

| 変数 | 既定 | 説明 |
| --- | --- | --- |
| `IMMICH_BASE_URL` / `IMMICH_API_KEY` | – | **必須**。Immich の URL と API キー |
| `GPMC_AUTH_DATA` | – | **必須**。Google フォトアプリの auth data（`GP_AUTH_DATA` / 旧 `GOTOHP_AUTH_STRING` も可） |
| `GPMC_AUTH_DATA_FILE` | `<GPMC_CACHE_DIR>/auth_data` | `creds add` が書き出す auth data ファイル（`GPMC_AUTH_DATA` が空のとき使用。モード 0600） |
| `GPMC_THREADS` | `3` | gpmc の並列アップロード数 |
| `GPMC_TIMEOUT` | `60` | gpmc の 1 リクエストあたりのタイムアウト（秒） |
| `GPMC_CACHE_DIR` | `/config` | gpmc のハッシュキャッシュ（`~/.gpmc`）の置き場所 |
| `GPMC_FORCE_UPLOAD` / `GPMC_SKIP_EXISTING_FILENAMES` | `false` | 重複排除の挙動（強制再アップ / 同名ファイルのスキップ） |
| `GPMC_USE_QUOTA` / `GPMC_SAVER` | `false` | 容量を消費する画質設定 |
| `GPMC_LOG_LEVEL` | – | gpmc 自身のログレベル（未指定なら `ERROR`、`LOG_LEVEL=DEBUG` なら `DEBUG`） |
| `GPMC_PROXY` / `GPMC_LANGUAGE` | – | gpmc のプロキシ（`protocol://user:pass@host:port`）と API の言語 |
| `GPMC_SHOW_PROGRESS` | `false` | gpmc の進捗表示を出す（デバッグ用） |
| `ALBUM_BACKEND` | `gpmc` | `gpmc` / `library_api` / `none`（旧 `gotohp` も `gpmc` として解釈） |
| `ALBUM_NAME_TEMPLATE` | `{album}` | 例: `Immich / {album}` |
| `BACKFILL_ALBUMS` | `true` | 既にアップロード済みの資産にも後からアルバムを付け直す |
| `ALBUM_INCLUDE_SHARED` | `false` | Immich の共有アルバムも対象に含める |
| `METADATA_BACKEND` | `embed` | `embed`（exiftool で埋め込み） / `library_api` / `both` / `none` |
| `ASSET_TYPES` | `IMAGE,VIDEO` | 対象の種類 |
| `INCLUDE_ARCHIVED` | `true` | Immich のアーカイブ済みも対象に含める |
| `FULL_SCAN` | `false` | 透かしを無視して毎回全件照合 |
| `MAX_ASSETS_PER_RUN` | `0`（無制限） | 1 回の実行で扱う上限 |
| `UPLOAD_BATCH_SIZE` | `200` | ステージングとアップロードのバッチ件数（`/work` の使用量に直結） |
| `KEEP_LOCAL_COPIES` | `false` | `/work` の一時コピーを実行後も残す（デバッグ用） |
| `WATERMARK_SKEW_MINUTES` | `10` | 差分検出の透かしを巻き戻す猶予（分） |
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
   `gpmc.Client.upload()` をプロセス内で呼びます（サブプロセス・pty・TUI 解析は不要）。
   返り値の `{パス: MediaKey}` を状態 DB に保存し、アルバムへは MediaKey を別途追加します
   （初回は `add_to_album` で作成し、以降は保存した album key に `add_to_existing_album`）。
   同名衝突は `名前_<assetId 先頭 8 桁>.ext` に退避します。
5. **冪等性** — 2 回目以降は「アップロード済み」「アルバム反映済み」の資産をスキップ。
   Google 側のハッシュ重複排除が働いた場合も `MediaKey` が返るため、アルバム付与は正しく行われます。

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

ネットワーク不要のエンドツーエンドテストが入っています（モック Immich + 偽の `gpmc` パッケージ）。

```bash
python3 -m venv /tmp/venv && /tmp/venv/bin/pip install -r requirements.txt
bash tests/run_e2e.sh        # 単体テスト + 3 回連続実行で、サイドカー・状態 DB・アルバム・冪等性を検証
python3 tests/test_google_auth.py   # oauth_token → auth data の変換だけを単体で検証（ネットワーク不要）
```

`run_e2e.sh` は既定で `/tmp/venv/bin/python` を使います。システムの Python で動かすときは
`PY=python3 bash tests/run_e2e.sh` のように `PY` を渡してください（CI も同じ方法です）。
`exiftool` があるとメタデータ埋め込みまで検証されます。

## トラブルシューティング

| 症状 | 対処 |
| --- | --- |
| `doctor` の `gpmc_credentials` が NG | `creds add -` で auth data を作り直す（失効が有力）。`/config` が永続化されているか確認 |
| `No email value in auth_data` | `GPMC_AUTH_DATA` に `oauth2_4/...` のクッキーをそのまま入れています。`creds add -` で auth data に交換してください |
| `exiftool` が NG | `METADATA_BACKEND=none` にするか、`docker compose pull` でイメージを取り直す（`libimage-exiftool-perl` 入り） |
| `docker compose pull` が `denied` / `unauthorized` | GHCR パッケージが private。`read:packages` の PAT で `docker login ghcr.io` するか、パッケージを public にする |
| Immich 同居時に設定が反映されない | `sidecar.env` が compose ファイルと同じディレクトリにあるか、`-f docker-compose.yml -f docker-compose.immich.yml` を付けているか確認 |
| アップロードが遅い/詰まる | `GPMC_THREADS` を下げる、`GPMC_TIMEOUT` を上げる、`GPMC_LOG_LEVEL=DEBUG` で gpmc のログを見る |
| Library API が 403 | 上記の 2025-03-31 制約。`ALBUM_BACKEND=gpmc` / `METADATA_BACKEND=embed` に戻す |
| `/work` が膨らむ | `MAX_ASSETS_PER_RUN` と `UPLOAD_BATCH_SIZE` を下げる。`KEEP_LOCAL_COPIES=false` を維持 |
| 全部やり直したい | `/state/sidecar-state.sqlite3` を消す（Google 側のハッシュ重複排除で二重アップロードは避けられます） |

## 制限事項

- gpmc は Google の**非公式**モバイル API を使います。仕様変更やアカウント制限のリスクは利用者が負います。
- Google フォトは EXIF/XMP の一部（タグ・人物・レーティング等）を UI に反映しません。原本の完全な情報は
  `/sidecar` のサイドカーファイル側に保持されます。
- 共有アルバムは既定で除外（`ALBUM_INCLUDE_SHARED=true` で対象化）。
- Immich 側の削除はミラーされません（Google フォト側は残ります）。
