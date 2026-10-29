# Ralph実行状態形式の移行

## 対象

Role単位の実行記録、pauses/<role>.json、quota.json、runner.pidを使う環境から、Principal × Role単位の実行記録とruntime.jsonを使う環境へ切り替えるときに使用する。

## 手順

1. 対象ProjectのRunnerを停止し、Agent・Git処理が終了していることを確認する。稼働中の旧Runnerと新しい実行形式を混在させない。
2. `.ralph/config.json`、`.ralph/github-state/`、`.ralph/pauses/`、`.ralph/quota.json`と作業差分を退避する。各RoleのagentNameが保存済み実行のPrincipalに対応していることを確認する。
3. 更新したRalphを起動するか、停止を解除する場合は`ralph resume <role>`を実行する。取り込み先はconfigの既定Principalであり、CLIのPrincipal上書き先へ既存作業を付け替えない。
4. `.ralph/runtime.json`と`.ralph/github-state/<principal>/<role>.json`で、Pause・Quota・Task・Claim・Worktree・保留判断が引き継がれていることを確認する。
5. 取り込み済み実行のWorktreeは元の場所で作業を続ける。以降の新しい作業は現行のPrincipal別パスを使用する。

元の実行記録は`.json.migrated`として保持する。旧Pause・Quotaは参照用に残し、取り込み後の更新先はruntime.jsonとする。既存のralph.logとローテーション済みログも変更・削除せず保持する。

Runnerの稼働を検証できない場合や、取り込み先と元の実行記録が食い違う場合は処理を止める。実行記録・結果・Worktreeを削除して移行エラーを回避しない。

現行の保存先・CLI・復旧方法は[Ralphの実行ガイド](../../ralph/README.md)を参照する。
