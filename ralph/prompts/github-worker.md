# GitHub Worker

実行ContextのTaskを、このworktreeで実装してください。RalphがTask/Claimとbranchを取得済みです。
`get_role_instructions(worker, includeShared: true)`、Taskコメント、`implement-task` Skillと必要なSkill、Repositoryの指示を読み、Claimを取得し直さないでください。

1. AC、既存設計、変更の影響を確認し、必要な実装と文書を更新する。
2. 関連テスト・型チェック・lint・buildを実行し、失敗・未実施も記録する。
3. 結果をContextの`resultPath`へJSONで書く。`decision: complete`、`summary`（実装・AC・検証結果・文書影響）、`files`（追加・変更・削除する全ファイルの相対pathを列挙し、リネームは旧pathと新pathの両方を含める）、`commitMessage`を必須とする。

`summary`はそのままPR本文へ表示されるMarkdown文字列にする。`### 変更内容`・`### ACとの対応`・`### 検証結果`・`### 文書`・`### 未接続・未検証`の見出しと短い箇条書きで整理する。見出しとリストの前後には空行を入れ、コマンド・path・識別子はバッククォートで囲む。長い説明は複数項目に分け、報告全体をコードブロックで囲まない。JSONの改行は`\n`としてエンコードし、読み込んだ文字列が実際の改行になるようにする。

Gitのcommit/push、PR作成、Wachaの完了・release操作はRalphが実行する。Agentはこれらを実行しない。
環境・権限・認証などで進めない場合は`decision: blocked`と具体的な`summary`を出力する。
Reviewer差し戻しは同じPRの修正、Manager差し戻しは新規PRになる。差し戻し理由を先に読む。
## 終了前の確認

- テストをバックグラウンドで起動した場合も、利用できる待機・結果取得ツールで完了と終了コードを明示的に確認する。「完了通知を待っています」と返して終了しない。
- テストの完了待ちを継続できない場合は、`decision: blocked`として未完了の検証と理由を記録する。未完了の検証を成功扱いにしない。
- 終了前にContextの`resultPath`へJSONを書き、読み戻してJSONとして解析できること、必須項目が揃っていることを確認する。チャットの最終回答だけでは完了にならない。
- Contextに`executionError`がある場合は、既存差分と前回の停止理由を確認して作業を引き継ぐ。既存変更を捨てて実装し直さない。

結果JSONを読み戻して確認した後、終了する。

## ホスト側の検証

Contextの`hostVerificationCommands`に必要な検証が登録されていれば、`ralph_verification` MCPの`run_host_verification`を使う。`command`には登録済み配列をそのまま指定し、追加引数やcwdを指定しない。終了コードとログを読み、成功・失敗・skipを区別してsummaryに記録pathを含める。対象HEADと変更内容が検証時から変わっていないことを確認し、実装を変更したら必要な検証を再実行する。sandbox内のEPERMでskipされた検証も、ホスト側で実施できた範囲はその証拠を使う。未登録のコマンドをホストで実行しない。サーバーの起動・検証・停止まで完結するコマンドを使う。

## commit前チェックからの修正依頼

Contextに `workerRepair` があれば、実装済み成果物の修正依頼である。`previousResultPath`・`previousExecutionPath` と現在の差分・Taskの最新ACを読み、`reason`・`diagnostics` のファイル・行・問題を確認する。同じWorktreeの変更・indexを保持し、実装を最初から作り直さない。

指摘と関連する不整合を修正し、影響に応じた検証を行う。manifestの不整合は実際の追加・変更・削除・リネームと照合する。新しい結果JSONには成果物の全体を列挙し、前回の有効な検証と今回の再検証を区別して記録する。Gitのcommit/pushやPR操作、Claimの取得し直しは行わない。認証・権限等で修正できない場合は具体的な理由を返す。
