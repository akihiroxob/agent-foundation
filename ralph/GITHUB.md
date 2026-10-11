# GitHub Workflow

ManagerのUI受入用のPlaywright MCP接続・検証用Server設定・証跡・終了管理は [Managerの実ブラウザ検証](BROWSER.md) を参照する。

## 役割と実行境界

RalphはWachaのClaimを取得し、Taskごとのworktreeを準備する。Agentは実装・レビュー・AC確認を行い、JSONで判断と根拠を返す。Gitのcommit/push、GitHubのPR作成・レビューコメント・merge、Wachaの状態遷移はホスト側のRalphが行う。Codexの`.git`書き込み制限を無効化する必要はない。

Workerは専用branchへ変更をコミットし、PRを作成する。ReviewerはPR head SHAを固定したworktreeで確認し、指定したCIチェックの成功後に（未指定ならCI確認なしで）PRコメントでのレビュー判定とSHAを指定したマージを行う。Managerはmerge commitをcheckoutし、ACを独立に確認する。

Reviewer差し戻しは同じopen PRを修正する。Manager差し戻しは最新の統合先branchから新しいPRを作成する。既存PR・merge SHA・レビュー結果はWachaに保持する。

## 設定

1. WachaのProject詳細でGitHub連携を設定する。`doing`・`in_review`・`wait_accept`のTaskは完了または差し戻してから切り替える。
2. Wacha Serverの`WACHA_GITHUB_TOKEN`に対象repositoryの読み取り資格情報を設定する。
3. GitHubの統合先branchを保護する。指定したCIチェック、PR経由の変更、会話の解決、管理者を含む保護を有効にする。保護なしのbranchで自律マージを運用しない。
4. 同一GitHubアカウントのPATを使用できる。GitHubの「承認レビュー必須」は設定せず、WachaのWorker・Reviewer・Managerを別Principalにする。RalphはGitのfetch/pushにも各RoleのPATを使用する。既存originはSSH形式でもよく、Ralphの通信だけを明示的なHTTPS URLで実行する。Cloneし直し・origin変更・`gh`・credential helperの永続設定は不要。
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

共通PATにはContents・Pull requestsの書き込みを付与する。CIチェックを指定する場合はChecks・Commit statusesの読み取りも付与する。PRコメントの操作にはPull requestsの書き込み権限を使用する。ManagerはGitHub APIを書き換えず、Wachaの検証済みPR情報とGitのmerge commitを使用する。Git fetchのために共通PATを渡す。Role別のPATならManagerは対象RepositoryのContents読み取り権限でよい。

Gitの資格情報は各fetch/pushの子プロセス環境と一時credential helperで渡し、HTTPSの対象hostとRepository pathを照合する。Gitの認証helperやHTTP headerの既存設定はその操作に限って上書きし、credential保存・対話入力・redirect・traceを使用しない。GitのURL書き換えが別接続先やSSHへ変換する場合は停止する。

Tokenの値を設定ファイル・Repository・ログへ入れない。資格情報はホスト環境へ設定する。設定したtoken環境変数と`GH_TOKEN`・`GITHUB_TOKEN`・`WACHA_GITHUB_TOKEN`はAgentプロセスへ渡さない。

`.ralph/worktrees/`、`.ralph/github-state/`、`.ralph/pauses/`、`.ralph/quota.json`、`.ralph/runner.pid`、`.ralph-result.json`はGit管理しない。1つのcheckoutには1つのRunnerを起動する。複数Runnerを並行運用する場合は別checkout・別設定・別Agent名を使用する。Wachaの排他的ClaimがTaskの重複作業を防ぐ。

```bash
python3 scripts/install_ralph.py --global
cd /path/to/project
ralph run
```

## Project別のホスト検証

GitHub方式では、Project rootの`.ralph/config.json`に、Agentのsandbox外で実行してよい検証コマンドを定義できる。

```json
{
  "verification": {
    "hostCommands": [
      ["npm", "run", "test:http"],
      ["node", "--import", "tsx", "--test", "server/tests/http.test.ts"]
    ]
  }
}
```

コマンド例は利用先のscript・test fileに合わせる。既定は空配列で、未設定時はホスト実行を公開しない。設定したコマンドを自動で全件実行するのではなく、Agentが`ralph_verification` MCPの`run_host_verification({ command: [...] })`から必要なものを選ぶ。Projectの `.mcp.json` の追加MCPと、Claimに固定した検証用MCPを共通処理でまとめ、Claude Code・Codexへ配信する。ProviderはMCP名に応じた選択を行わない。設定方法は [MCP設定](MCP.md) を参照する。検証は同期的に終了を待ち、Claude CodeのMCP自動バックグラウンド化も無効にする（Claude Code 2.1.212以降）。

コマンドと全引数を完全一致で照合し、cwdはClaimに紐づく登録済みTask Worktreeに固定する。shellによる文字列展開・連結は行わない。検証プロセスにはPATH・HOME・locale等の基本環境だけを渡し、PAT・Wacha Credential・SSH_AUTH_SOCK等は渡さない。設定はホスト側の正本を毎回読み、Worktree内の設定を許可元にしない。

これは指定コマンド全体のホスト実行許可であり、listenだけの権限追加ではない。コマンドから実行されるProjectのscript・テストコードもホスト上で動く。サーバーを起動したまま待つコマンドより、起動・検証・停止まで完結する検証scriptを登録する。1回の上限は60分で、終了・timeout・中断時は残った子プロセスも停止する。実行中はClaimを更新し、期限切れの別Claimへの切り替えは行わない。

終了コード、Task・Claim・Role、cwd、HEAD、変更内容のfingerprint、開始・終了時刻、stdout/stderrのログをProject rootの`.ralph/verification/<claimId>/`に保持する。実行結果をAgentへ返し、Agentはログ中の失敗・skipと検証対象の一致を確認して判断する。製品コードを変更した場合は必要な検証を再実行する。ホスト実行の許可はテスト成功や受入の自動承認を意味しない。

## レビューの証拠

Reviewerの`approve`はGitHubのApprove APIを呼ばず、通常のPRコメントへ検証結果を投稿する。コメントにTask・Review Claim・head SHA・判定を示すmarkerを付ける。RalphはコメントIDを保存してSHAを指定してマージし、WachaはGitHub上のコメントとcurrent Claimを再照合する。同じGitHubアカウントでも、Wacha上の自己レビュー禁止は維持される。

GitHub方式のPromptを変更する場合はRole設定に`githubPrompt`を指定する。相対pathはProject rootを基準にする。`prompt`はローカル方式のPromptに使用する。

## Agentの出力

`summary`はMarkdown文字列で記載する。見出し・空行・箇条書きで変更内容、ACとの対応、検証、文書、未検証事項を整理し、コマンドやpathはバッククォートで囲む。JSONを読み込んだ後の改行は実際の改行とし、報告全体をコードブロックで囲まない。WorkerのPR本文とReviewerのPRコメントはMarkdownを保持して表示し、Task・Reviewer・head SHAは本文末尾の箇条書きにまとめる。

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

`files`にはコミットする追加・変更・削除の全パスを記載する。リネーム時は旧パスと新パスの両方を含める。`git mv`・`git rm`でステージ済みの旧パスは再度`git add`しないが、削除対象として一覧と照合する。存在せずステージ済み削除でもないパスと、一覧外の変更は拒否する。

Reviewerは`decision: approve | reject`、Managerは`decision: accept | reject`と根拠を含む`summary`を出力する。全Roleで、環境問題は`decision: blocked`と原因を含む`summary`を出力する。

Workerが指定していない変更、Runnerのファイル、worktree外を指すpathはコミットしない。Reviewer/Managerが追跡済みファイルを変更した状態の判断は反映しない。Agent側の検証結果はReviewとCIでも確認する。

## 再試行と復旧

- `.ralph/github-state/<role>.json`にClaim、worktree、PR番号、commit SHA、判断、最終Wacha操作を保存する。
- Agent実行中は60秒間隔でClaimを更新する。CI待ちでも判断を保持しClaimを更新する。CI待ちのためにAgentを再起動しない。
- push・PR作成・レビューコメント・merge・Wacha更新の途中で通信が途絶した場合は、保存済み情報・コメントID・GitHub状態・WachaのrequestIdを使って再実行する。
- 古いheadへのReview、CI未成功、current Review Claimに紐づかないコメントをマージ条件として扱わない。GitHubのbranch保護を最終的な競合防止に使用する。
- 環境問題や無効な判断ではRoleを停止し、再起動後も停止を保持する。Token制限はProviderと`quotaKey`単位で待機期限を共有する。同じProviderでも別契約・別アカウントならRole設定の`quotaKey`を分ける。
- Agentはバックグラウンド検証の完了と終了コードを明示的に取得し、結果JSONを書いて読み戻した後に終了する。通知待ちや最終回答だけで終了しない。検証を完了できない場合は`blocked`として記録する。
- 結果JSONの欠落・不正などで結果反映が失敗した場合は、Claim・Worktree・実行記録を保持してRoleを停止する。`executionError`に反映処理の停止理由と時刻を記録し、Contextにも渡す。`ralph resume <role>`で既存のWorktreeと変更を引き継いで続行する。成功後に通常の完了処理を行う。明示的な`blocked`判断ではClaimを解放する。
- 保存済み実行のClaimが期限切れなら、実行準備または結果反映時に同じTask・RoleのClaimを自動再取得する。Workerは期限切れで再取得可能な`doing`、Reviewerは`in_review`、Managerは`wait_accept`に限定し、要件の変更も確認する。他Agentの有効Claim、Taskの段階変更、要件変更があれば実行記録とWorktreeを保持する。通信失敗は再試行し、再取得のrequestIdを保存して二重取得を防ぐ。Agent実行中のheartbeatは更新だけを行い、別Claimへ勝手に切り替えない。
- 再取得時は`previousClaimIds`に旧Claimを、`worktreeClaimId`に元のWorktree識別子を保持する。branch・変更・結果を引き継ぎ、Reviewerのコメントは新しいClaimに紐づけ直す。最終Wacha操作は旧requestIdの成功receiptを先に確認し、成功していなければ新ClaimでGitHub状態を再検査して反映する。PR headが変わっている場合は既存判断を反映しない。
- ローカル方式でも進捗なし・通常エラーが`retry.maxNoProgressAttempts`（既定3）回続いた場合はRoleを停止する。

原因を解消したら再開する:

```bash
ralph resume worker
```

Claimを再取得できない、PR headが変更された、Agent結果が無効などで保存済み実行を継続できない場合は、現在の実行を解放・保存したうえで再選択する。未送信の変更やPR状態を先に確認する。

```bash
ralph resume reviewer --discard-execution
```

この操作はClaimを解放し、実行情報をtimestamp付きJSONへ退避する。worktree・branch・PRは削除しない。新しいClaimで再検証する。WachaやGitHubへ接続できない場合は再試行し、実行情報を消さない。

## Worktreeの掃除

WorkerのPR提出、Reviewerの承認・差し戻し、Managerの受入・差し戻しについて、最終Wacha操作が成功した後に、その実行のworktreeを自動削除する。branchとPRは保持し、差し戻し後はPRの最新headからworktreeを再作成する。

結果・Claim・PR・SHA・最終操作は`.ralph/github-state/completed/<role>-<claimId>.json`へ退避する。通信途絶後は同じrequestIdで最終操作の成功を確認してから掃除する。掃除失敗はTaskの成功を取り消さず、次のRole実行準備で完了済みworktreeの掃除だけを再試行する。

想定した登録済みworktree・SHAと一致し、追跡済み変更や未追跡ファイルがない場合だけ`git worktree remove`を実行する。Ralph生成の`.ralph/mcp.json`・`.ralph-result.json`は結果退避後に削除する。Gitで除外した依存関係やbuild成果物もworktreeと共に削除されるため、残す必要がある成果物はworktree外へ保存する。

実行中、CI待ち、通信失敗、blocked、`--discard-execution`で退避した実行、未保存変更・未知のファイル・追加commit・lockがあるworktreeは保持する。削除失敗の理由はログと完了記録に残る。自動的な`reset --hard`・`clean`・強制削除・branch削除は行わない。この仕組みの導入前に完了記録なしで残ったworktreeは、自動削除の対象にしない。
