# Immich の docker compose に同居させる

既存の Immich スタックへこのサイドカーを足すときの手順と、`.env` の扱いのまとめ。

## 1. イメージは GHCR から取る

`main` への push / `v*` タグ / 手動実行（workflow_dispatch）で GitHub Actions が
E2E テスト → `linux/amd64` + `linux/arm64` のビルド → GHCR への push を自動実行する。

| タグ | 内容 |
| --- | --- |
| `ghcr.io/nmt3325/immich-gphotos-sidecar:latest` | `main` の最新ビルド |
| `:main` | ブランチ名タグ（`latest` と同じ内容） |
| `:sha-<短縮SHA>` | コミット単位で固定したいとき |
| `:v1.2.3` / `:1.2` | `v*` タグを push したとき |

本番運用では `latest` ではなく `:sha-...` か `:v...` で固定するのが安全。

リポジトリが private の間は GHCR のパッケージも private なので、pull 側で認証が必要。

```bash
echo "<PAT (read:packages)>" | docker login ghcr.io -u <github-user> --password-stdin
```

認証なしで pull したい場合は GitHub の
`リポジトリ → Packages → immich-gphotos-sidecar → Package settings → Change visibility`
で public にする（リポジトリ本体を public にしなくても、パッケージだけ public にできる）。

## 2. `.env` はどうするか

Compose の環境変数には役割の違う入口が 3 つある。ここを分けて考えるのが要点。

| 仕組み | 何に使われるか | ファイル名 | 補足 |
| --- | --- | --- | --- |
| `.env`（自動読み込み） | compose ファイル内の `${VAR}` の**補間**と `docker compose` 自身の設定 | プロジェクトディレクトリの `.env` 固定（**1 つだけ**） | `--env-file` で差し替え可 |
| `env_file:` | **コンテナへ渡す環境変数** | 任意の名前・複数指定可 | 補間には使われない |
| `environment:` | コンテナへ渡す環境変数（最優先） | – | `${...}` で `.env` の値を参照できる |

つまり **サイドカーの設定は `env_file: ./sidecar.env` で渡せば、Immich の `.env` とは
完全に別管理**になる。`.env` を 2 つ置く必要はない（自動で読まれる `.env` はプロジェクトに
1 つだけなので、2 つ置いても片方は無視される）。

### Immich の `.env` に追記するのは避ける

- Immich の `immich-server` / `immich-machine-learning` は `env_file: .env` を読むので、
  `GPMC_AUTH_DATA` や `IMMICH_API_KEY` が Immich のコンテナにも渡ってしまう。
- `TZ` のような同名キーの取り合いになる。
- Immich 公式の `example.env` を追従更新するときに差分が汚れる。

### 推奨レイアウト

```
/opt/immich/
├── docker-compose.yml          # Immich 公式（変更しない）
├── docker-compose.immich.yml   # このリポジトリの追加定義
├── .env                        # Immich 用（変更しない）
├── sidecar.env                 # サイドカー用（.env.example をコピー）
└── sidecar/                    # サイドカーの永続データ
    ├── state/                  #   sqlite（必須）
    ├── sidecar/                #   サイドカー JSON/XMP・レポート（必須）
    └── config/                 #   gpmc のキャッシュと認証情報（必須）
```

```bash
cd /opt/immich
cp <this-repo>/docker-compose.immich.yml .
cp <this-repo>/.env.example sidecar.env
$EDITOR sidecar.env    # IMMICH_API_KEY / GPMC_AUTH_DATA / スケジュールなど
# IMMICH_BASE_URL は docker-compose.immich.yml 側で http://immich-server:2283 に固定済み

docker compose -f docker-compose.yml -f docker-compose.immich.yml pull
docker compose -f docker-compose.yml -f docker-compose.immich.yml run --rm immich-gphotos-sidecar doctor
docker compose -f docker-compose.yml -f docker-compose.immich.yml up -d
```

`-f` を毎回書きたくない場合は次のどちらか。

- Immich の `.env` に `COMPOSE_FILE=docker-compose.yml:docker-compose.immich.yml` を追記する
  （`.env` は補間と CLI 設定用のファイルなので、これは `.env` の本来の使い方）
- Immich の `docker-compose.yml` の先頭に `include:` を足す（Compose v2.20+）

  ```yaml
  include:
    - docker-compose.immich.yml
  ```

## 3. 別プロジェクトとして分離する場合

Immich と別フォルダ・別プロジェクトで動かすなら、Immich のネットワークへ外部参加させる。
この場合はサイドカー側のフォルダに自由に `.env` を置ける（衝突しない）。

```yaml
services:
  immich-gphotos-sidecar:
    image: ghcr.io/nmt3325/immich-gphotos-sidecar:latest
    container_name: immich-gphotos-sidecar
    restart: unless-stopped
    env_file:
      - .env
    environment:
      IMMICH_BASE_URL: http://immich-server:2283
    networks:
      - immich
    volumes:
      - ./data/state:/state
      - ./data/sidecar:/sidecar
      - ./data/config:/config

networks:
  immich:
    name: immich_default   # Immich 側のネットワーク名（<プロジェクト名>_default）
    external: true
```

ネットワーク名は `docker network ls` で確認する。external network を使わない場合は
`IMMICH_BASE_URL=http://<ホストの IP>:2283` のようにホスト経由で指定する。

## 4. 更新

```bash
docker compose -f docker-compose.yml -f docker-compose.immich.yml pull immich-gphotos-sidecar
docker compose -f docker-compose.yml -f docker-compose.immich.yml up -d immich-gphotos-sidecar
```

`latest` を追う運用なら上記のまま。固定運用ならタグを書き換えてから同じコマンドを実行する。
自分でビルドしたい場合は `docker-compose.example.yml` の `build:` セクションを使う。
