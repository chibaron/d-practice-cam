# Firebase に送る（隠し機能）

d-practice-cam はスタンドアローン（ネット無し、PC だけ）で使うのが基本です。このページは、計測したラップを **Firebase Realtime Database にも送る隠し機能**の設定です。
送ったラップは、今の閲覧ページでそのまま見られます。README と公開用の exe にはこの機能の説明を入れていません。

設定しなければ Firebase は使わず、画面にも設定画面にも Firebase の項目は出ません。

## 設定のしかた

exe の隣に、次の2つのファイルを置きます。

1. **鍵 `serviceAccount.json`**: Firebase のサービスアカウントの鍵（JSON）。Firebase コンソールの「プロジェクトの設定 → サービスアカウント」で作れます
2. **`firebase.yaml`**: `firebase.example.yaml` をコピーして名前を変え、中身を書きます

```yaml
credentials: ./serviceAccount.json   # 鍵ファイルのパス（鍵の中身は書かない）
database_url: https://<プロジェクト>-default-rtdb.<リージョン>.firebasedatabase.app/
root: practice           # 書き込み先のルート（. $ # [ ] / と空は使えない）
auth_uid: python-script
timeout_sec: 10          # 送信をあきらめるまでの秒数
```

置いたらアプリを起動し直します。画面上部が `Firebase: OK`（緑）になれば送れています。

- どちらかが無い、または `database_url` が空のときは、Firebase を使わずスタンドアローンで動きます（画面にも何も出ません）
- 鍵と `firebase.yaml` は人に渡したり、公開したり、Git に入れたりしないでください
- データベースのURLは設定画面では変えられません。`firebase.yaml` に直接書きます
- 書き込み先のルートと鍵ファイルのパスは、設定画面（S キー）の General タブでも変えられます（`firebase.yaml` があるときだけ）。鍵ファイルの変更は起動し直すと反映されます

## 書き込む形式

日付ごとのノードに、ラップを1件ずつ追記します。

```
{root}/laps_2026-10-31/!trackSetting   = { "minLap": 10, ... }
{root}/laps_2026-10-31/{pushId}        = { "time": "14:05:27", "pilotName": "CHO!", "lapTime": 15.64 }
```

| キー | 中身 |
| --- | --- |
| `time` | 通過したときの PC の時刻（`HH:MM:SS`） |
| `pilotName` | QR で読んだパイロット名。読んでいない・クリアになった ch はバンド名（F1 など） |
| `lapTime` | ラップ（秒、小数2桁）。`999` はスタート（起点） |

- 起動したときと計測を始めたときに、その日の日付ノードを確認し、無ければ `config.yaml` の `track_setting` の値で `!trackSetting` を書いて作ります。すでにあれば上書きしません
- `!trackSetting` はほかの端末で変えるとすぐ反映されます。設定画面の Course タブで変えると、その日の日付ノードにも書き込みます
- `minLap` 以下の通過は送りません

## 画面上部の表示

`firebase.yaml` と鍵があるときだけ出ます（どちらか無ければ何も出ません）。

| 表示 | 意味 |
| --- | --- |
| `Firebase: OK`（緑） | 送れている |
| `Firebase: OFFLINE`（赤） | ネットにつながらない |
| `Firebase: KEY ERR`（赤） | 鍵の形式が違うなど |

## ネットが切れたとき

- 計測・音・アナウンス・画面のタイム・ランキングは止まりません。`minLap` などのコース設定は `config.yaml` の値を使います
- 送信は1回だけ試し、`timeout_sec`（10秒）で返事が無ければあきらめます。**送れなかったラップは、あとからも送りません。** 内容はログ（`d-practice-cam.log`）に残ります
- 次のラップが送れたら `OK` に戻り、日付ノードの確認と `!trackSetting` の読み込みをやり直します
