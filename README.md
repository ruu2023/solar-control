# solar-control

Renogy Rover 40A (MPPT充電コントローラー) を Bluetooth Low Energy (BLE) 経由で監視・制御し、
あわせて EcoFlow DELTA 2 (家庭用ポータブルバッテリー) の稼働状況を EcoFlow 公式 REST API 経由で
取得するアプリ。情報取得（ソーラー側: バッテリー/PV/負荷のテレメトリ、家庭用バッテリー側: 残量/入出力
ワット数/温度）と、Rover の DC負荷出力のON/OFF制御を行う。

> 旧 `app/home-bttery` プロジェクト（EcoFlow REST APIクライアント）はこのアプリに統合済み。
> `solar_control/ecoflow_client.py` がその移植版で、`status` コマンド / `/status` エンドポイントが
> ソーラー側・家庭用バッテリー側の両方を1つのレスポンスにまとめて返す。

Docker Desktop for Mac は BLE をコンテナにパススルーできないため、このアプリは **Docker を使わずホストMac上でネイティブに動かす**構成にしている（bleakがmacOSのCoreBluetoothを直接叩く）。

## ハードウェア要件

- Renogy Rover 40A 本体
- **Renogy BT-1 モジュール**（Rover側面のRJ12ポート「RS232」に挿す小型Bluetoothアダプタ。単体売りされている）
  - Rover自体にはBLEが内蔵されていないため、この外付けモジュールが必須
  - BT-2モジュール（RS485用、主にDCC50S/スマートバッテリー向け）でも動くことがあるが、Roverには基本BT-1を使う
- 実行するMac（このマシン）がBT-1の電波到達範囲内にあること
- macOSのBluetooth権限をターミナル/実行プロセスに許可する必要あり（システム設定 → プライバシーとセキュリティ → Bluetooth）

## 通信の仕組み（リサーチ結果）

Rover自体はシリアルのModbus RTUで喋っており、BT-1モジュールはそれをBLE(GATT)にブリッジしているだけ。フレーム形式はModbus RTUと同一（device_id + function + payload + CRC16）で、輸送経路がBLE notifyに変わるだけ。

- Write service UUID: `0000ffd0-0000-1000-8000-00805f9b34fb`
- Write characteristic UUID: `0000ffd1-0000-1000-8000-00805f9b34fb`
- Notify characteristic UUID: `0000fff1-0000-1000-8000-00805f9b34fb`（応答はここに1回のnotifyで返る）
- デバイス名prefix: `BT-TH-...`（BT-1）

### レジスタマップ（Renogy公式MODBUS仕様 + コミュニティ実装([cyrils/renogy-bt](https://github.com/cyrils/renogy-bt))で検証済み）

| 用途 | レジスタ | ワード数 | function |
|---|---|---|---|
| デバイス情報（型番文字列） | 12 (0x000C) | 8 | 3 (read) |
| デバイスアドレス | 26 (0x001A) | 1 | 3 (read) |
| 充電情報ブロック（バッテリー%/電圧/電流、温度、負荷V/I/P、PV V/I/P、当日累積など） | 256 (0x0100) | 34 | 3 (read) |
| バッテリータイプ | 57348 (0xE004) | 1 | 3 (read) |
| **負荷ON/OFF制御** | 266 (0x010A) | - | 6 (write single register, 0=off/1=on) |

device_id はコントローラー単体接続なら `255` 固定でよい（ハブ/デイジーチェーン構成の場合のみ変更）。

CRC16はModbus標準（poly 0xA001, init 0xFFFF, 下位バイト→上位バイトの順で送信）。`solar_control/protocol.py` の実装は既知のModbusテストベクタ、および cyrils/renogy-bt のテーブル実装と出力が一致することを確認済み。

## EcoFlow (家庭用バッテリー) 連携

`solar_control/ecoflow_client.py` が EcoFlow 公式 REST API (HMAC-SHA256署名) 経由で DELTA 2 の
`device/quota/all` を叩き、残量/入出力ワット数/温度を取得する。BLEとは無関係な素のHTTPS呼び出し
なので、SSH経由のセッションからでも問題なく動作する（BLE操作のようなmacOSローカル権限の制約はない）。

- ベースURL: `https://api-e.ecoflow.com`（EU/APAC。リージョンによっては `api-a.ecoflow.com`）
- 認証: `accessKey`/`secretKey` をリクエストパラメータと共にHMAC-SHA256署名し、
  `accessKey`/`nonce`/`timestamp`/`sign` ヘッダーを付与
- `.env` が未設定（`ECOFLOW_ACCESS_KEY`等が空）の場合でも `status` / `/status` は動作し、
  `home_battery` セクションに `{"error": "..."}` を返すだけでソーラー側の取得は継続する

## セットアップ

```bash
cd app/solar-control
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

macOSでBluetoothの権限ダイアログが出たら許可する（初回のみ）。

### 1. BT-1モジュールのMACアドレスを探す

```bash
python -m solar_control scan
```

見つかった `XX:XX:XX:XX:XX:XX  BT-TH-XXXXXXXX` を `.env` の `RENOGY_MAC_ADDRESS` に設定する。

### 1.5 EcoFlow の accessKey/secretKey/デバイスSNを設定（任意）

[EcoFlow開発者ポータル](https://developer-eu.ecoflow.com)で発行した `accessKey`/`secretKey` と、
対象デバイスのシリアル番号を `.env` の `ECOFLOW_ACCESS_KEY` / `ECOFLOW_SECRET_KEY` /
`ECOFLOW_DEVICE_SN` に設定する。シリアル番号が分からない場合は以下で一覧取得できる。

```bash
python -c "
from dotenv import load_dotenv; load_dotenv()
import os, json
from solar_control.ecoflow_client import EcoFlowClient
c = EcoFlowClient(os.environ['ECOFLOW_ACCESS_KEY'], os.environ['ECOFLOW_SECRET_KEY'])
print(json.dumps(c.get_device_list(), indent=2, ensure_ascii=False))
"
```

未設定のままでも `status` / `/status` は動作し、`home_battery` セクションが
`{"error": "..."}` になるだけ。

### 2. REST APIサーバー起動

BLEはRover側が同時に1接続しか受け付けないため、**`serve`が唯一のBLE接続の主体**になる構成にしている。
`status`/`load`のCLIはこの`serve`が起動している前提のHTTPクライアントとして動く（下記参照）。

```bash
python -m solar_control serve
```

| Method | Path | 説明 |
|---|---|---|
| GET | `/health` | BLE接続状態 |
| GET | `/status` | `{"solar": {...}, "home_battery": {...}}` — ソーラー(Rover)と家庭用バッテリー(EcoFlow)のテレメトリをまとめて取得 |
| POST | `/load` | `{"on": true}` / `{"on": false}` で負荷をON/OFF |

### 3. 状態取得（CLI）

`serve`を起動した状態で、別ターミナルから:

```bash
python -m solar_control status
```

中身は`GET /status`を叩くだけの薄いHTTPクライアント。ソーラー側(Rover, BLE)と家庭用バッテリー側
(EcoFlow, REST)を1つのJSONにまとめて表示する。

```json
{
  "solar": {
    "battery_percent": 54, "battery_voltage": 12.2, "battery_current": 3.74,
    "load_status": "off", "charging_status": "mppt", "...": "..."
  },
  "home_battery": {
    "battery_percent": 42.5, "input_watts": 0, "output_watts": 0, "temperature": 27
  }
}
```

`serve`が起動していない場合は、BLEの二重接続を試みる代わりに分かりやすいエラーを表示する:

```
Could not reach the solar-control API server at http://127.0.0.1:8000. Make sure `python -m solar_control serve` is running.
```

### 4. 負荷ON/OFF（CLI）

こちらも`POST /load`を叩くだけのHTTPクライアント（`serve`が起動している必要がある）。

```bash
python -m solar_control load on
python -m solar_control load off
```

`scan`だけは特定デバイスへの接続を確立しないため、これまで通り直接BLEでスキャンする
（`serve`が起動中でも競合しない）。

## 常駐化（launchd）

Macにログインするたびに`serve`を自動起動し、クラッシュしたら自動再起動させたい場合は、
`launchd/com.solar-control.api.plist` をLaunchAgentとして登録する。

**重要**: 必ず `~/Library/LaunchAgents/`（ユーザーのLaunchAgent、GUIログインセッションに紐づく）に
置くこと。`/Library/LaunchDaemons/`（システムデーモン）にすると、SSH経由の実行と同様にmacOSの
Bluetooth(TCC)権限が付与されず`BleakError: BLE is not authorized`になる可能性が高い。

```bash
cp launchd/com.solar-control.api.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.solar-control.api.plist
```

- ログは `launchd/solar-control.out.log` / `launchd/solar-control.err.log` に出力される。
- 停止・再登録解除:
  ```bash
  launchctl unload ~/Library/LaunchAgents/com.solar-control.api.plist
  ```
- plist内のパスはこのリポジトリの絶対パス（`/Volumes/docker-storage/docker-apps/app/solar-control`）
  にハードコードしてあるので、リポジトリを移動した場合は書き換えが必要。
- **未検証**: launchd経由で起動したプロセスに対して、これまでTerminal.appから許可していたBluetooth
  権限が引き継がれるかどうかは実際に試すまで分からない。もし`scan`と同様の`BleakError: BLE is not
  authorized`が出た場合は、システム設定 → プライバシーとセキュリティ → Bluetooth の一覧に
  launchd経由のプロセス（venvのpython3）が出ているか確認し、許可する必要があるかもしれない。

## テスト

BLEやEcoFlowの実機・実APIなしで検証できる部分（CRC16・フレーム組み立て、EcoFlow署名生成・レスポンス整形）のみユニットテストを用意。

```bash
pytest tests/
```

## 未実装 / 今後の拡張候補

- ~~launchd等によるMac常駐化~~ → `launchd/com.solar-control.api.plist` を追加済み（登録は未検証、上記「常駐化」参照）
- ~~CLIがBLE接続を二重に持つ問題~~ → `status`/`load`は`serve`へのHTTPクライアント化で解消済み
- BLE切断時の自動再接続・リトライ（`serve`は起動時に1回接続するのみで、稼働中に切れた場合の
  自動復旧はまだ無い。launchdの`KeepAlive`はプロセスがクラッシュした場合のみ効く）
- 定期ポーリング・履歴保存（DB/CSV蓄積してグラフ化するような用途にはまだ対応していない）
- 認証（このAPIはローカルネットワーク限定利用を想定しており認証なし。常駐化して稼働時間が伸びるほど
  この点は要検討）
- Raspberry Piなど専用機での常時稼働（その場合はDocker化も現実的になる）
- Web UI（現状はREST API + CLIのみ）

## 進捗メモ（作業再開用）

セッションが切れても状況が追えるよう、ここに現在地を記録する。

- 2026-07-14: コード一式（`protocol.py` / `ble_manager.py` / `rover_client.py` / `api.py` / `cli.py` / tests）を作成済み。
- 2026-07-14: `.venv` は作成済みだが、依存パッケージ（bleak/fastapi/uvicorn/pydantic/python-dotenv/pytest）は**未インストール**だった。
- 2026-07-14 15:29〜: `source .venv/bin/activate && pip install -r requirements.txt` を実行開始（バックグラウンド、PID 3481）。完了未確認。
- **未実施**: `.env` 未作成（`.env.example` をコピーして `RENOGY_MAC_ADDRESS` を設定する必要あり）。
- **次にやること**:
  1. `pip install` が完了しているか確認（`pip list` で bleak/fastapi/pytest が入っているか）
  2. `cp .env.example .env`
  3. `python -m solar_control scan` を実行し、`BT-TH-...` デバイスのMACアドレスを探す（BT-1モジュールがRoverに挿さっており、電波到達範囲内にいることを確認）
  4. 見つかったMACアドレスを `.env` の `RENOGY_MAC_ADDRESS` に設定
  5. `python -m solar_control status` で疎通確認 → 成功したら `load on`/`load off` の実機テスト
- 実機（Renogy Rover本体・BT-1モジュール）を使った検証はまだ一度も成功していない（scanすら未実行）。

### 2026-07-14 続報: 依存インストール完了 → scanでBluetooth権限エラー

- `pip install -r requirements.txt` 完了（bleak 1.1.1 / fastapi 0.128.8 / pytest 8.4.2 等）。
- `pytest tests/` は4件全てpass。
- `.env` を `.env.example` からコピー済み（`RENOGY_MAC_ADDRESS` はまだ空欄）。
- `python -m solar_control scan` を実行 → **`BleakError: BLE is not authorized - check macOS privacy settings`** で失敗。
- 原因調査: この作業セッションは **SSH経由**（`sshd-session` の子プロセスとしてシェルが動いている）で実行されている。macOSのBluetooth権限はGUIの許可ダイアログを介して付与されるため、SSH越しのヘッドレスプロセスにはダイアログが出ず、CoreBluetoothが未許可のまま止まっている。
- **次にやること（要ユーザー対応）**:
  1. Macの実機（GUIログインしている画面）で システム設定 → プライバシーとセキュリティ → Bluetooth を開く
  2. SSHセッションから起動したプロセス（おそらく親プロセスは Terminal.app、または `sshd-session` 経由の場合は一覧に出てこない可能性あり）にBluetoothアクセスを許可
     - 一覧に対象が出ない場合は、一度Macのローカル画面（SSHでなく直接ログインしたTerminal）から `python -m solar_control scan` を実行して権限ダイアログを表示させ、許可する必要があるかもしれない
  3. 許可後、SSH経由の本セッションから再度 `python -m solar_control scan` を実行して認識できるか確認
  4. デバイスが見つかったらMACアドレスを `.env` の `RENOGY_MAC_ADDRESS` に設定 → `status` → `load on/off` の順で検証

### 2026-07-14 続報2: ローカルTerminalではscan成功 → デバイス発見 → SSH側は依然権限エラー

- ユーザーがMacのローカルTerminal（GUIログイン画面、venv手動有効化）で `python -m solar_control scan` を実行 → 成功。権限ダイアログを許可し、デバイスを発見。
  ```
  D4A59CD1-4FCC-6162-F061-04E1E62B7DB4  BT-TH-6A6AFEBB
  ```
- `.env` の `RENOGY_MAC_ADDRESS` に上記UUIDを設定済み。
- **確定した制約**: macOSのBluetooth(TCC)権限は「責任元プロセス」単位で付与される。ローカルTerminal.appから起動したpythonと、SSHセッション（`sshd-session`配下）から起動したpythonは**別の責任元とみなされ、権限が引き継がれない**。SSH側で再度scanしたが同じ `BleakError: BLE is not authorized` で失敗を確認済み。
- **結論**: BLEに実際に触れる操作（`scan` / `status` / `load on|off` / `serve` でのBLE通信）は、**このSSH経由の作業セッションでは実行不可**。今後は毎回ユーザーにローカルTerminalでのコマンド実行と結果貼り付けを依頼する運用とする。コード変更・テスト（`pytest`、BLE非依存部分）は引き続きSSH側で対応可能。
- **次にやること**: ユーザーのローカルTerminalで以下を実行してもらい、結果を共有してもらう。
  ```bash
  source .venv/bin/activate
  python -m solar_control status
  ```
  疎通確認できたら、`load on` / `load off` の実機テストに進む。

### 2026-07-14 続報3: statusコマンド成功（実機テレメトリ取得OK）

- ユーザーのローカルTerminalで `python -m solar_control status` を実行 → 成功。実機からテレメトリを正常取得。
  ```json
  {
    "battery_percent": 54, "battery_voltage": 12.2, "battery_current": 3.74,
    "controller_temperature": 32, "battery_temperature": 28,
    "load_voltage": 0.0, "load_current": 0.0, "load_power": 0,
    "pv_voltage": 37.8, "pv_current": 1.26, "pv_power": 48,
    "max_charge_power_today": 300, "max_discharge_power_today": 198,
    "charge_amp_hours_today": 73, "discharge_amp_hours_today": 69,
    "power_generation_today": 928, "power_consumption_today": 866,
    "power_generation_total_wh": 627254,
    "load_status": "off", "charging_status": "mppt"
  }
  ```
- 読み取り系（charge_info / device_info系レジスタ、CRC16、フレーム解析）はこれで実機検証済みと言える。
- 現在の負荷(load)状態は `off`。
- **次にやること**: `load on` / `load off` の書き込み系（レジスタ266書き込み）を実機で検証する。ユーザーのローカルTerminalで以下を順に実行してもらう。
  ```bash
  python -m solar_control load on
  python -m solar_control status   # load_status が "on" になっているか確認
  python -m solar_control load off
  python -m solar_control status   # load_status が "off" に戻るか確認
  ```
  負荷に実際に何か（照明など）が接続されていれば、ON/OFFで物理的な動作も確認できるとより確実。

### 2026-07-14 続報4: load on/off で異常発生 → レスポンス検証を追加して調査中

実機テストでバグを発見:

- `python -m solar_control load on` → `{"load_status": "off"}` と誤って表示（が、直後の `status` 確認では実際には `load_status: "on"` に変わっており、書き込み自体は成功していた）。
- 続けて `python -m solar_control load off` → `IndexError: index out of range`（`rover_client.py:53` の `response[5]` で異常終了）。

**原因**: `set_load()` が応答フレームの長さ・CRC・Modbus例外ビットを一切検証せず `response[5]` に決め打ちでアクセスしていた。Modbus例外応答は5バイトしかない（`id, function|0x80, exception_code, crc_lo, crc_hi`）ため、応答が正常な8バイトのechoでない場合に誤った値を読んだりインデックス範囲外になったりしていた。

**対応（コード修正済み、実機再検証はこれから）**:
- `protocol.py` に `validate_response(data, expected_function)` を追加。長さ不足・CRC不一致・Modbus例外ビット・関数コード不一致を検出して `ModbusError` を送出し、生バイト列(hex)をメッセージに含めるようにした。
- `rover_client.py` の `get_status()` / `set_load()` の両方で応答受信直後にこれを通すよう変更。
- `cli.py` で `ModbusError` を捕捉し、スタックトレースではなく `Bad response from device: ...` の分かりやすいメッセージを表示するよう変更。
- `tests/test_protocol.py` に `validate_response` のユニットテストを追加（正常系・短すぎるフレーム・CRC不一致・Modbus例外の4パターン）。全8テストpass確認済み。
- **未確認**: 実機で `load on` / `load off` を再実行して、実際にどのバイト列が返ってきているか（`ModbusError` のhexダンプで正体を特定する）。CRC不一致なのか、Modbus例外なのか、単に短いだけなのかはまだ分かっていない。BT-1モジュールが接続直後の書き込みでは応答が不安定という可能性も含め、次のログで切り分ける。
- **次にやること**: ユーザーのローカルTerminalで以下を再実行し、結果（特にエラーになった場合の `ModbusError` メッセージ全文）を共有してもらう。
  ```bash
  python -m solar_control load on
  python -m solar_control status
  python -m solar_control load off
  python -m solar_control status
  ```

### 2026-07-14 続報5: `app/home-bttery`（EcoFlow DELTA 2 REST APIクライアント）を統合

`app/home-bttery` にあった EcoFlow DELTA 2 監視コードを `solar-control` に統合し、`home-bttery` ディレクトリは削除した。

- `solar_control/ecoflow_client.py` として `EcoFlowClient`（HMAC-SHA256署名クライアント）を移植。
  `parse_status()` と `get_status(serial_number)` を追加し、quotaレスポンスから
  `battery_percent`/`input_watts`/`output_watts`/`temperature` を抜き出す処理を共通化した。
- `config.py` に `ECOFLOW_ACCESS_KEY`/`ECOFLOW_SECRET_KEY`/`ECOFLOW_DEVICE_SN`/`ECOFLOW_BASE_URL`
  と `has_ecoflow` プロパティを追加（未設定でも動作するようオプション扱い）。
- `status` コマンド / `/status` エンドポイントを、ソーラー(Rover)側・家庭用バッテリー(EcoFlow)側の
  両方を1回の呼び出しで取得し `{"solar": {...}, "home_battery": {...}}` として返す形に統合。
  片方が失敗・未設定でももう片方は取得を継続する（`{"error": "..."}` を該当セクションに入れる）。
- `requirements.txt` に `requests` を追加。`.env` / `.env.example` に EcoFlow用の変数を追加
  （実際の accessKey/secretKey/デバイスSNは `home-bttery/.env` から `.env` に移行済み）。
- `tests/test_ecoflow_client.py` を追加（`_flatten`・署名生成・`parse_status` のユニットテスト、
  実API呼び出しなしで検証可能）。`pytest tests/` 全13件pass確認済み。
- EcoFlowのREST API呼び出しはBLEと違い素のHTTPS通信なので、SSH経由の作業セッションからでも
  実際にAPIを叩いて動作確認済み（`battery_percent: 42.5` を取得。上記のBLE権限制約はこちらには
  適用されない）。
- `python -m solar_control status` の実機（Rover側BLE）再検証は、macOSのBluetooth(TCC)権限制約に
  より引き続きユーザーのローカルTerminalでの実行が必要（継続してこの運用のまま）。
- また、同じセッション中に「別Macから今のMacにSSHで接続して`scan`を実行」しても同じ
  `BleakError: BLE is not authorized`になることを確認した。つまりこの制約はClaude Codeの
  SSHセッションに限らず、**SSH経由であれば接続元がどこであっても**当てはまる（GUIセッションに
  直接紐づいたプロセスでないとBLE権限が付かない）。プロンプトの見た目だけでは「ローカルか
  SSH経由か」は判別できないので、実行環境を疑うときはまずそこを確認する。

### 2026-07-14 続報6: `serve`常駐化 + CLIの純粋API化

BLEはRover側が同時1接続までしか受け付けないため、常駐サーバー(`serve`)がBLE接続を持ち続ける
運用にすると、CLIの`status`/`load`が独自にBLE接続を開こうとして衝突する問題があった。これを
解消するため以下を実施:

- `cli.py`の`status`/`load`を、直接BLEに繋ぐのをやめて`serve`（`http://127.0.0.1:{API_PORT}`）への
  薄いHTTPクライアントに書き換えた。`RoverClient`/`EcoFlowClient`/`ModbusError`の直接importは
  cli.pyから削除（すべて`api.py`側に一本化）。`serve`が起動していない場合は
  `requests.ConnectionError`を捕まえて分かりやすいメッセージを出す。
- `scan`は特定デバイスへの接続を確立しない（探索のみ）ため、引き続き直接BLEを使う。
- macOSログイン時の自動起動・クラッシュ時再起動のため、`launchd/com.solar-control.api.plist`
  （LaunchAgent）を追加。`~/Library/LaunchAgents/`に置いて`launchctl load`する運用で、
  `/Library/LaunchDaemons/`（システムデーモン、GUIセッションと無関係）ではなく必ずLaunchAgent
  にする必要がある — でないとBLE権限がSSHと同じ理由で通らない可能性が高い。
- **未検証**: launchd経由（LaunchAgent）で起動したプロセスが、これまでTerminal.appから許可した
  Bluetooth権限を実際に引き継げるかどうか。TCCの権限は実行バイナリ（`.venv/bin/python3`）単位で
  紐づいている可能性があるため、動く可能性は高いと考えているが未確認。
- BLE切断時の自動再接続・リトライは今回のスコープ外（未実装のまま）。launchdの`KeepAlive`は
  プロセスがクラッシュ/終了した場合のみ再起動するので、「接続は切れたがプロセスは生きている」
  状態からの自動復旧にはならない。
- **次にやること**: ユーザーのローカルTerminal（Macの画面に直接ログインしたセッション）で
  1. `cp launchd/com.solar-control.api.plist ~/Library/LaunchAgents/`
  2. `launchctl load ~/Library/LaunchAgents/com.solar-control.api.plist`
  3. `launchd/solar-control.err.log`を見て`BleakError: BLE is not authorized`が出ていないか確認
  4. 出ていたらシステム設定→プライバシーとセキュリティ→Bluetoothで許可、出ていなければ
     `python -m solar_control status`（別ターミナルから）で疎通確認
