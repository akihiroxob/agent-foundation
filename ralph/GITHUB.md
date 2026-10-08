# GitHub Workflow

## 役割と実行境界

RalphはWachaのClaimを取得し、Taskごとのworktreeを準備する。Agentは実装・レビュー・AC確認を行い、JSONで判断と根拠を返す。Gitのcommit/push、GitHubのPR作成・レビューコメント・merge、Wachaの状態遷移はホスト側のRalphが行う。Codexの`.git`書き込み制限を無効化する必要はない。

Workerは専用branchへ変更をコミットし、PRを作成する。ReviewerはPR head SHAを固定したworktreeで確認し、指定したCIチェックの成功後に（未指定ならCI確認なしで）PRコメントでのレビュー判定とSHAを指定したマージを行う。Managerはmerge commitをcheckoutし、ACを独立に確認する。

Reviewer差し戻しは同じopen PRを修正する。Manager差し戻しは最新の統合先branchから新しいPRを作成する。既存PR・merge SHA・レビュー結果はWachaに保持する。

## 設定

1. WachaのProject詳細でGitHub連携を設定する。`doing`・`in_review`・`wait_accept`のTaskは完了または差し戻してから切り替える。
2. Wacha Serverの`WACHA_GITHUB_TOKEN`に対象repositoryの読み取り資格情報を設定する。
3. GitHubの統合先branchを保護する。指定したCIチェック、PR経由の変更、会話の解決、管理者を含む保護を有効にする。保護なしのbranchで自律マージを運用しない。
4. 同一GitHubアカウントのPATを使用できる。GitHubの「承認レビュー必須」は設定せず、WachaのWorker・Reviewer・Managerを別Principalにする。Git pushの資格情報も対象repositoryへのpush権限が必要。
5. `.ralph/config.json`に次を設定する。Repository・統合先branch・CIチェック名の正本はWachaであり、Ralphの`origin`は同じrepositoryでなければならない。CI確認は任意で、Wachaのチェック名を空欄（`requiredChecks: []`）にすると省略する。チェック名を指定した場合は全件成功を必須とする。GitHub側で必須チェックが設定されている場合、その条件は引き続き適用される。

```json
{
  "github": {
    "enabled": true,
    "tokenEnvs": {
      "worker": "RALPH_GITHUB_TOKEN",
      "reviewer": "RALPH_GITHUB_TOKEN",
      "manager": "RALPH_GITHUB_TOKEN"
    },
    "setupCommands": [["npm", "ci"]]
  }
}
```

`setupCommands`はworktree準備後にホスト側で1回実行する、shellを使わないコマンド引数配列。利用先に必要な依存関係の準備を指定する。不要なら空配列とする。

共通PATにはContents・Pull requestsの書き込みとChecks・Commit statusesの読み取りを付与する。PRコメントの操作にはPull requestsの書き込み権限を使用する。ManagerはGitHubを書き換えず、Wachaの検証済みPR情報とGitのmerge commitを使用するため、Manager tokenの設定は必須ではない。

Tokenの値を設定ファイル・Repository・ログへ入れない。資格情報はホスト環境へ設定する。設定したtoken環境変数と`GH_TOKEN`・`GITHUB_TOKEN`・`WACHA_GITHUB_TOKEN`はAgentプロセスへ渡さない。

`.ralph/worktrees/`、`.ralph/github-state/`、`.ralph/pauses/`、`.ralph/quota.json`、`.ralph/runner.pid`、`.ralph-result.json`はGit管理しない。1つのcheckoutには1つのRunnerを起動する。複数Runnerを並行運用する場合は別checkout・別設定・別Agent名を使用する。Wachaの排他的ClaimがTaskの重複作業を防ぐ。

```bash
python3 scripts/install_ralph.py --global
cd /path/to/project
ralph run
```

## レビューの証拠

Reviewerの`approve`はGitHubのApprove APIを呼ばず、通常のPRコメントへ検証結果を投稿する。コメントにTask・Review Claim・head SHA・判定を示すmarkerを付ける。RalphはコメントIDを保存してSHAを指定してマージし、WachaはGitHub上のコメントとcurrent Claimを再照合する。同じGitHubアカウントでも、Wacha上の自己レビュー禁止は維持される。

GitHub方式のPromptを変更する場合はRole設定に`githubPrompt`を指定する。相対pathはProject rootを基準にする。`prompt`はローカル方式のPromptに使用する。

## Agentの出力

RalphがPromptへTask、Claim、worktree、固定SHA、`resultPath`を渡す。AgentはClaimを取得し直さず、Git/GitHub操作・Wachaの状態遷移を実行しない。

Worker:

```json
{
  "decision": "complete",
  "summary": "実装内容、ACとの対応、テスト結果、未実施、文書影響",
  "files": ["src/example.ts", "test/example.test.ts"],
  "commitMessage": "Implement example"
}
```

Reviewerは`decision: approve | reject`、Managerは`decision: accept | reject`と根拠を含む`summary`を出力する。全Roleで、環境問題は`decision: blocked`と原因を含む`summary`を出力する。

Workerが指定していない変更、Runnerのファイル、worktree外を指すpathはコミットしない。Reviewer/Managerが追跡済みファイルを変更した状態の判断は反映しない。Agent側の検証結果はReviewとCIでも確認する。

## 再試行と復旧

- `.ralph/github-state/<role>.json`にClaim、worktree、PR番号、commit SHA、判断、最終Wacha操作を保存する。
- Agent実行中は60秒間隔でClaimを更新する。CI待ちでも判断を保持しClaimを更新する。CI待ちのためにAgentを再起動しない。
- push・PR作成・レビューコメント・merge・Wacha更新の途中で通信が途絶した場合は、保存済み情報・コメントID・GitHub状態・WachaのrequestIdを使って再実行する。
- 古いheadへのReview、CI未成功、current Review Claimに紐づかないコメントをマージ条件として扱わない。GitHubのbranch保護を最終的な競合防止に使用する。
- 環境問題や無効な判断ではRoleを停止し、再起動後も停止を保持する。Token制限はProviderと`quotaKey`単位で待機期限を共有する。同じProviderでも別契約・別アカウントならRole設定の`quotaKey`を分ける。
- ローカル方式でも進捗なし・通常エラーが`retry.maxNoProgressAttempts`（既定3）回続いた場合はRoleを停止する。

原因を解消したら再開する:

```bash
ralph resume worker
```

Claim期限切れ、PR head変更、無効なAgent結果などで保存済み実行を継続できない場合は、現在の実行を解放・保存したうえで再選択する。未送信の変更やPR状態を先に確認する。

```bash
ralph resume reviewer --discard-execution
```

この操作はClaimを解放し、実行情報をtimestamp付きJSONへ退避する。worktree・branch・PRは削除しない。新しいClaimで再検証する。WachaやGitHubへ接続できない場合は再試行し、実行情報を消さない。

worktreeの削除は受入・差し戻し履歴・未送信変更を確認した後に保守作業として行う。自動的な`reset --hard`・`clean`・branch削除は行わない。
