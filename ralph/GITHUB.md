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

`.ralph/worktrees/`、`.ralph/github-state/`、`.ralph/runtime.json`、`.ralph/runtime.lock`、`.ralph/runners/`、`.ralph/logs/`、`.ralph/verification/`、`.ralph-result.json`はGit管理しない。GitHub方式では同じcheckoutで別Principalの同じRoleと、同じPrincipalの別Roleを並行起動できる。同じPrincipal・同じRoleの重複起動だけを拒否し、Wachaの排他的ClaimがTaskの重複作業を防ぐ。CLI・共有状態は[実行ガイド](README.md#principal別の並行実行)を参照する。

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

コマンド例は利用先のscript・test fileに合わせる。既定は空配列で、未設定時はホスト実行を公開しない。設定したコマンドを自動で全件実行するのではなく、Agentが`ralph_verification` MCPの`run_host_verification({ command: [...] })`から必要なものを選ぶ。Projectの `.mcp.json` の追加MCPと、Claimに固定した検証用MCPを共通処理でまとめ、Claude Code・Codexへ配信する。ProviderはMCP名に応じた選択を行わない。設定方法は [MCP設定](MCP.md) を参照する。検証の子プロセスへMCPのstdinを継承させず、同期的に終了を待ち、Claude CodeのMCP自動バックグラウンド化も無効にする（Claude Code 2.1.212以降）。

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

- `.ralph/github-state/<principal>/<role>.json`にClaim、worktree、PR番号、commit SHA、判断、最終Wacha操作を保存する。
- Agent実行中は60秒間隔でClaimを更新する。CI待ちでも判断を保持しClaimを更新する。CI待ちのためにAgentを再起動しない。
- 通信断は成否不明として扱う。commitはClaim trailerとSHA、pushは同じSHAとremoteの整合、PR・レビューコメントは既存resourceと識別marker、mergeは現在のPR状態、Wachaは保存したrequestIdのreceiptで照合する。PR・コメント作成の成否不明記録があるのに照合先が見つからない場合、同じPOSTを再送しない。
- 古いheadへのReview、CI未成功、current Review Claimに紐づかないコメントをマージ条件として扱わない。GitHubのbranch保護を最終的な競合防止に使用する。
- 登録済みWorktreeに紐づく操作エラーは復旧Agentへ返す。Taskの保留はRole全体を停止しない。Worktreeの所有を確認できない場合や、実行準備の構成自体が不正な場合はRoleを停止する。Token制限はProviderと`quotaKey`単位で待機期限を共有する。
- Agentはバックグラウンド検証の完了と終了コードを明示的に取得し、結果JSONを書いて読み戻した後に終了する。通知待ちや最終回答だけで終了しない。検証を完了できない場合は`blocked`として記録する。
- 結果JSONの欠落・不正などの失敗はClaim・Worktree・前回結果を保持して復旧Agentに診断を渡す。明示的な`blocked`判断ではClaimを解放し、そのTaskを保留する。成否不明の操作を保留するときはClaim解放を追加実行せず、記録とWorktreeを保持する。
- 保存済み実行のClaimが期限切れなら、実行準備または結果反映時に同じTask・RoleのClaimを自動再取得する。Workerは期限切れで再取得可能な`doing`、Reviewerは`in_review`、Managerは`wait_accept`に限定し、要件の変更も確認する。他Agentの有効Claim、Taskの段階変更、要件変更があれば実行記録とWorktreeを保持する。通信失敗は再試行し、再取得のrequestIdを保存して二重取得を防ぐ。Agent実行中のheartbeatは更新だけを行い、別Claimへ勝手に切り替えない。
- 再取得時は`previousClaimIds`に旧Claimを、`worktreeClaimId`に元のWorktree識別子を保持する。branch・変更・結果を引き継ぎ、Reviewerのコメントは新しいClaimに紐づけ直す。最終Wacha操作は旧requestIdの成功receiptを先に確認し、成功していなければ新ClaimでGitHub状態を再検査して反映する。PR headが変わっている場合は既存判断を反映しない。
- ローカル方式でも進捗なし・通常エラーが`retry.maxNoProgressAttempts`（既定3）回続いた場合はRoleを停止する。

原因を解消したら再開する:

```bash
ralph resume worker --principal worker-a
```

Claimを再取得できない、PR headが変更された、Agent結果が無効などで保存済み実行を継続できない場合は、現在の実行を解放・保存したうえで再選択する。未送信の変更やPR状態を先に確認する。

```bash
ralph resume reviewer --principal reviewer-a --discard-execution
```

この操作は対象Principal・Roleが停止中であることをOSロックで確認し、Claimを解放して実行記録を`github-state/<principal>/retained/`へ退避する。変更のないWorktreeは照合後に削除し、未保存変更や未知のファイルがある固定パスのWorktreeは`worktrees/<principal>/retained/<role>-<claimId>`へ移動して保持する。branch・PRは削除しない。新しいClaimで再検証する。WachaやGitHubへ接続できない場合は再試行し、実行情報を消さない。

## Worktreeの掃除

WorkerのPR提出、Reviewerの承認・差し戻し、Managerの受入・差し戻しについて、最終Wacha操作が成功した後に、その実行のworktreeを自動削除する。branchとPRは保持し、差し戻し後はPRの最新headからworktreeを再作成する。

結果・Claim・PR・SHA・最終操作は`.ralph/github-state/<principal>/completed/<role>-<claimId>.json`へ退避する。通信途絶後は同じrequestIdで最終操作の成功を確認してから掃除する。掃除失敗はTaskの成功を取り消さず、Roleの監視周期ごとに、Taskの有無・Pauseの有無にかかわらず、そのPrincipal・Roleの完了済みWorktreeの掃除だけを再試行する。

想定した登録済みworktree・SHAと一致し、追跡済み変更や未追跡ファイルがない場合だけ`git worktree remove`を実行する。Ralph生成の`.ralph/mcp.json`・`.ralph-result.json`は結果退避後に削除する。`.ralph/tmp/node-compile-cache/`は生成キャッシュとして削除する。追跡済みファイルやsymlinkがある場合は削除しない。その他の未知の未追跡ファイルは自動削除しない。Gitで除外した依存関係やbuild成果物もworktreeと共に削除されるため、残す必要がある成果物はworktree外へ保存する。

実行中、CI待ち、通信失敗、未保存変更・未知のファイル・追加commit・lockがあるWorktreeは保持する。blockedまたは明示的な破棄でClaim解放が確認できた実行は、根拠・結果・実行記録を退避した後、変更のないWorktreeを削除する。削除失敗の理由はログと完了記録に残る。自動的な`reset --hard`・`clean`・強制削除・branch削除は行わない。完了記録と対象Claimに照合できないWorktreeは自動削除しない。

コミット対象のパスはWorktree内の正規の相対パスに限定する。`.git`自体・Ralphの実行用ファイル・Agent設定は保護し、`.gitignore`や`.github/`は通常の成果物として扱う。

競合解消のpending mergeでは、追跡済みの`.ralph/config.json`と`.mcp.json`を取り込める。ただし`MERGE_HEAD`が設定済みbase branchのremote追跡refの祖先で、通常ファイルのindexのblobとmodeがそのcommitと一致し、作業ファイルにも変更がない場合だけ許可する。許可した設定は再stageせず、検証済みのindexをそのままcommitする。Workerによる設定変更・symlink・base外のcommit・mergeのない取り込みは拒否し、対象パスをエラーへ示す。実行の許可元には引き続きホスト側の設定を使う。

## commit前エラーのWorkerによる自動修正

Workerの結果確認後、commit前の空白・conflict markerチェック、成果物manifestの必須項目・不足・実差分との不一致を検出した場合は、同じWorktreeのWorkerを再実行する。Workerへファイル・行・理由、元のTask / AC、現在の変更、前回の結果と実行記録の参照を渡す。会話履歴の再開は必須ではない。Workerは関連する修正・検証を行い、成果物全体の新しい結果JSONを返す。

過去の結果と実行記録はProject rootの `.ralph/verification/<worktreeClaimId>/worker-repair/<attempt>/` に保存する。Contextの `workerRepair` に参照・診断・試行回数を含める。`pendingResult` は退避後に解除し、新しい結果を読む。Worktree・index・未追跡ファイルは保持する。Claimは既存の更新・期限切れ再取得の規則に従い、別Task・変更された要件へ成果を流用しない。

自動修正の上限は既存の `retry.maxNoProgressAttempts`（既定3、正の整数）を利用する。回数はTaskの実行記録に保存し、Runner再起動でリセットしない。上限に達した場合は未解決の結果と差分を保持して停止する。GitHub helperの終了コード76は修正依頼の保存成功を示し、RunnerはRoleをpauseせず次のWorker実行へ進む。保存済みの結果を処理する経路でも同じ扱いとする。

通常Roleでの成果物修正はcommit・PR処理前に限定する。保護path、未知のGitエラー、認証・権限、commit後等の失敗も復旧Agentが診断するが、保護チェックの緩和や副作用後の成果物変更を許可しない。通信障害の再試行はreceipt照合を通す。Git診断はファイル・行・問題だけを返し、ソース行・remote URL・認証情報を出力しない。Git・PR操作と業務状態遷移は引き続きRalphが所有する。

## 並行実行時の競合とエラー表示

Gitの共通refや管理情報はWorktree間でも共有する。Git自身のロックで競合した場合は短く再試行し、継続する場合は保存済み実行を保持して通常の再試行へ戻る。ロックファイルを削除しない。ネットワーク・権限エラーや不明な失敗を同じ扱いにしない。

Reviewerの承認後にマージAPIが失敗した場合、対象headが変わっていないこととGitHubの`mergeable: false`で競合が確定していることを再確認する。確定した競合は`reviewRepair`に前回の結果・実行記録・head SHAを保存し、同じWorktreeのReviewerへ返す。ReviewerがRejectと再レビュー条件を記録し、Workerが修正する。判定待ち・保護ルール・通信障害・認証失敗をコンフリクトとしてRejectしない。再判定は`retry.maxNoProgressAttempts`までとし、超過時は成果と診断を保持して停止する。

失敗は`github-state/<principal>/<role>.error.json`へ処理段階・安全な理由・Task・Claim・PR番号・時刻を記録する。コンソールとPause理由には処理と原因、判明したTask・PRを表示する。Gitの生のstderr、認証情報、HTTP response本文は表示しない。未知のローカル例外は種類とローカル処理の失敗として表示する。明示的なblockedでは根拠と実行記録をretainedへ保持してClaimを解放する。Worktreeは変更がなければ削除し、未保存変更や未知のファイルがあれば保持する。

### Workerによる上流commitの取り込み

Workerが前提Taskの変更を取り込むためにHEADを進めた場合、Ralphは開始SHAが現在HEADの祖先であることと、現在HEADが新しくfetchしたリモートbase branchの祖先であることを確認する。両方が成立し、保存済みcommit・PR・最終操作がない場合だけ、開始SHAを現在HEADへ更新して記録する。Worktreeや差分は書き換えず、明示的なファイル一覧・保護対象・差分検査を通してTask用のcommitを作る。リモートbaseに含まれない無関係なcommitや開始SHAより前への巻き戻しは許可しない。復旧Agentの`revise`にも同じ確認を適用する。検証結果は実施したSHAに対応させる。

## Agentによる操作エラーの復旧

復旧の可否・次の行動は`prompts/github-recovery.md`を使うAgentが判断する。実行基盤は副作用の整合性・権限・回数と時間の上限を強制する。診断中のHEAD・index・製品差分・Role結果の変更は拒否する。

復旧Agentは専用の`.ralph/recovery-result.json`へ`action`とMarkdownの`summary`を返す。

- `retry`: 保存済み結果を保持して同じ操作へ戻す。GitHub・Wachaの照合と保護検証を再実行し、未確認の作成操作を再送しない。
- `revise`: 通常Roleへ成果物・判断の修正を返す。commit・PR番号・レビューコメント・最終Wacha操作が記録された後やWorkerのHEADが変わった後は拒否する。
- `defer`: 当該Taskを保留し、原因・必要な保守・再開条件を保存する。他のTaskを実行できる。

`retry.recovery.maxAttempts`は既定3回、`maxSeconds`は既定900秒。既存のWorker修正・Reviewer再判定も共通の予算を消費する。最初の障害からの経過時間を再起動後も引き継ぎ、Agent実行と子プロセスにも残り時間のtimeoutを適用する。同じ失敗と成果物・結果が繰り返された場合は進展なしとして保留する。Token数・金額の厳密な上限ではない。

根拠・前回の判断・診断結果は`.ralph/verification/<worktreeClaimId>/operation-recovery/<attempt>/`へ保存する。保留の実行記録は`.ralph/github-state/<principal>/deferred/`、Worktreeは`.ralph/worktrees/<principal>/deferred/<role>-<claimId>`へ保存する。該当Roleは他PrincipalでもそのTaskを再選択しない。Claim解放済みの`blocked`は、同じProjectの別Taskに保留時点より新しいAccepted更新があると、所有Principalが保存したWorktreeと証跡を引き継いで自動再評価する。Accepted更新を前提解消と決めつけず、Agentが再判断する。再び保留した場合は新しい保留時点を基準とし、同じ更新で再起動しない。成否不明の操作・復旧Agentによる保留は自動再開しない。Role選択時の実行可能数からも保留Taskを除外し、他のRoleに実行可能なTaskがあればそちらへ進む。`ralph status`でRunner・Agent・結果反映・保留理由と実行記録の場所を確認できる。

原因を解消して`ralph resume <role> --principal <principal>`を実行すると、同じPrincipalの保留Taskが次の実行準備で再開対象になる。再開対象は新規取得候補の有無にかかわらず復元する。保存したClaimがまだ有効で候補一覧に出ない場合も、現在の所有・期限・段階を照合して引き継ぐ。実行中のTaskがあればその終了後に引き継ぎ、保存したWorktree・操作結果を復元してClaim等を照合する。明示的なresumeで復旧予算を更新する。`blocked`で保留したTaskは、以前の判断を証跡へ退避し、新しいClaimでRoleを起動して再判定する。成否不明の操作の再試行では保存済み結果を保持して照合する。成否不明の結果を成功・失敗へ手動で書き換えない。
