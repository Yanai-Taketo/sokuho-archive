# data/

アーカイブの保存先です。中身は `sokuho_archive` が自動で書き込みます。

- `raw/` … 受信したXMLをそのまま保存した一次資料。**手で編集しないでください。**
- `events/` … 速報の出現・更新・消滅を記録した追記専用ログ (JSONL)。
- `latest.xml` … 最新の受信内容。
- `state.json` … ETagや現在表示中の速報など、アーカイブの状態。内容が変化したときだけ更新されます。
- `runtime.json` … アーカイバの死活記録。毎回の取得で更新されるためGit管理外です。
- `index.sqlite3` … 検索用インデックス。再生成できるためGit管理外です。

整合性は次のコマンドで検査できます。

```bash
PYTHONPATH=src python3 -m sokuho_archive verify
```
