# RunPod harness

RunPodでGPU検証を行うための道具です。Pod作成前に料金を確認し、削除用のタイマーを登録します。

## 構成

- `runpod_gpu_guard.py`：Podの作成前確認、料金・利用時間の制限、削除予約、終了後の削除を行うハーネスです。
- `test_runpod_gpu_guard.py`：課金なしで、料金上限、再試行、削除対象の取り違えなどを検証します。

## 現在の動作環境

Python 3.10以降とrunpodctlが必要です。Podの作成には、さらにLinux、systemd、root権限が必要です。

`create --confirm`では、最初にLinux、root権限、systemdへの接続を確認します。条件を満たさなければ`unsupported_environment`のJSONエラーで終了し、Podやタイマーは作成しません。`preflight`と見積もり表示には、この制限を設けません。

- runpodctlは既定で `/usr/local/bin/runpodctl` を呼びます。別の場所にある場合は、環境変数 `COEIROINK_RUNPODCTL` に実行ファイルの絶対パスを指定してください。
- 削除サービスは `/usr/bin/python3` と `/usr/bin/systemctl` を使い、`/etc/systemd/system/` に設定ファイルを作成します。
- 削除サービスはrootで動き、rootのrunpodctl認証設定を使います。そのため、Windows、macOS、systemdのないLinuxでは、この作成処理はそのままでは使えません。`preflight`だけならsystemdやroot権限は不要です。

認証はrunpodctl側の設定を使います。APIキーやSSH秘密鍵は、このリポジトリに置かないでください。

## 使い方

このスキルの`scripts/`ディレクトリで実行してください。

```bash
python3 runpod_gpu_guard.py --help
python3 runpod_gpu_guard.py preflight
python3 runpod_gpu_guard.py create --help
```

`preflight`は既存Podの確認だけを行います。`--gpu-id`を付けると、そのGPUの価格と在庫も確認します。Podの作成・停止・削除はしません。

`create`は、`--confirm`を付けた場合だけPodを作成します。付けなければ、条件確認と見積もり表示だけで終了します。

JSONの `planned_command` は確認用です。これを直接実行すると削除タイマーや料金確認を通らないため、実際の作成はハーネスに `--confirm` を付けて行ってください。

```bash
python3 runpod_gpu_guard.py create \
  --gpu-id "NVIDIA RTX A4000" \
  --image runpod/pytorch:1.0.3-cu1281-torch291-ubuntu2404 \
  --cloud-type COMMUNITY --public-ip \
  --max-hourly-usd 0.20 --delete-after-minutes 30
```

GPUは名前またはIDの完全一致で指定します。自動的に別のGPUへ変更することはありません。

CLIの在庫表示はクラウド別の値ではないため、JSONの`available`が`false`でも、料金などの条件を満たせば作成を試せます。その場合は警告を出し、実際の空き状況は作成APIの応答で判断します。

### 料金とストレージ

`--max-hourly-usd`は、新しく作る1 PodのGPU料金とストレージ見積もりの合計に対する時間単価の上限です。指定時間までの見積額もJSONに含めます。実際の割り当て価格が上限を超えた場合は、そのPodを削除し、削除を確認してから再試行します。

短時間の検証では、通常、追加の永続ボリュームは不要です。以下の2つは別のものです。

- `--container-disk-in-gb`：起動・作業用ディスクです。既定20GBで、正の整数を指定できます。
- `--volume-in-gb`：Podを停止しても残すボリュームです。既定0GBで、必要な場合だけ任意の容量を整数GBで指定します。Podの削除時には消えます。ネットワークボリュームの作成は扱いません。

ハーネスはストレージを`$0.10/GB/月`、1か月720時間として見積もります。この換算は請求額の保証ではありません。現在の料金は[RunPodの料金表](https://docs.runpod.io/pods/pricing)で確認してください。停止したままボリュームを残すとストレージ料金が続くため、検証後はPodを削除してください。

### 複数Pod

既に実行中・起動中のPodがあれば、通常は追加作成を拒否します。意図的に複数使う場合だけ `--allow-multiple` を付けてください。警告を標準エラーとJSONの `warnings` に出します。

料金上限は作成するPod単体のもので、既存Podを含むアカウント全体の上限ではありません。複数エージェント間の排他制御は行いません。他のPodを自動停止・削除することもありません。

### 再試行と削除予約

`--confirm`を付けた作成では、価格上限超過、在庫不足、APIのレート制限の場合に、10秒間隔で最大3回試します。3回には最初の試行も含みます。各試行で価格を取得し直しますが、料金上限を引き上げたり、登録済みの削除期限を延ばしたりはしません。条件確認だけの場合は再試行しません。

認証エラーや未知のJSON形式は再試行しません。作成がタイムアウトした場合や、作成済みか確認できない場合も、二重作成を避けるためエラーで終了します。作成処理後のエラーには、監視対象の名前、タイマー、確認できたPodのIDをJSONに含めます。Podが残っている場合は `preflight` で確認してください。

削除予約はローカルのsystemdだけで行います。`--delete-after-minutes`は既定30分、最大60分です。期限は削除処理の開始予定で、APIの応答などによって削除完了まで時間がかかることがあります。ローカル環境の停止中は監視できず、その間の課金継続は防げません。

RunPod側の `--terminate-after` は機能せず、公式CLIから削除されたため使用しません。[公式CLIの修正内容](https://github.com/runpod/runpodctl/pull/330)

### 出力と実行時間

`--help`を除き、標準出力は成功・失敗ともJSONを1つだけ返します。進捗と警告は標準エラーへ出します。成功は終了コード0、引数エラーは2、その他の失敗は1です。未知のJSON構造はエラーにし、空のPod一覧として扱いません。

runpodctlの各実行は既定30秒で打ち切ります。`--wait`を付けた作成では、SSH待機時間（`--wait-timeout`、既定90秒）を加えます。ただし、削除期限までの残り時間を超えて待ちません。`--wait`はSSHサーバーの応答を待つだけで、鍵認証の成功までは確認しません。

終了後の削除には、`cleanup`と対象PodのID、`--confirm`を指定します。削除前に必要な結果を保存してください。

## テスト

`scripts/`ディレクトリで実行してください。

```bash
python3 -B -m unittest -q
```

RunPodへの操作とsystemdへの設定はテスト内で置き換えます。実際のPodは作成せず、root権限も不要です。CLIの時間制限だけは、待機するローカルPythonプロセスを使って確認します。
