# Ralph Runner

WachaのTask状態を監視し、Claude CodeまたはCodexのWorker・Reviewer・最終受入専用Managerを1 Taskずつ使い捨てで起動するRunnerです。

GitHub方式の設定・資格情報・worktree・PR・受入・復旧・commit前エラーのWorker自動修正は [GitHub Workflow](GITHUB.md) を参照してください。

sandboxの影響箇所と現在の回避策は [Sandboxの影響と運用](SANDBOX.md) を参照してください。

## 責務

- Ralph Runner: Taskの有無の確認、エージェントプロセスの起動、失敗時の待機と再試行
- Wacha: Task、担当、進捗、レビュー状態の管理
- Agent provider: Claude Codeなどの実行環境固有の起動処理
- 利用先リポジトリ: プロジェクト固有の設定、仕様、知識

現在対応しているTask BackendはWacha、Agent ProviderはClaude CodeとCodexです。

## Globalインストール

このリポジトリのルートで実行します。

```bash
python3 scripts/install_ralph.py --global
```

既定ではRuntimeを`~/.local/share/agent-foundation/ralph/runtime/`、CLIを`~/.local/bin/ralph`へ配置します。`~/.local/bin`が`PATH`に含まれていない場合は追加してください。

利用先リポジトリで設定を初期化します。

```bash
cd /path/to/project
ralph init
# Codexを使う場合
ralph init --agent-provider codex
```

`ralph init`は次のRepo固有設定を作成または更新します。Claude用の2ファイルはClaude Provider選択時だけ作成します。

```text
.ralph/config.json       RalphとWachaの設定
.mcp.json                Claude CodeのWacha MCP接続設定（Claudeのみ）
.claude/settings.json    Wacha MCPの有効化とツール権限（Claudeのみ）
```

既存の`.mcp.json`と`.claude/settings.json`がある場合は、Wacha関連だけをマージし、その他のMCPサーバー、権限、設定を保持します。JSONが壊れている場合や既存フィールドの型が不正な場合は上書きせずに終了します。

## Repo-localインストール

チームやCIでRunnerのバージョンを固定したい場合は、利用先リポジトリへRuntimeを配置できます。

```bash
python3 scripts/install_ralph.py --target /path/to/project
```

利用先には次のファイルが作成されます。

```text
.mcp.json
.claude/
  settings.json
.ralph/
  config.json
  runtime/
    bin/ralph
    bin/ralph-loop
    backends/wacha.sh
    providers/claude.sh
    providers/codex.sh
    prompts/
```

既存の`.ralph/config.json`は上書きしません。Repo-localのRunner本体を更新する場合は、同じコマンドを再実行します。
自動実行には3つのRoleそれぞれの`agentName`と、Wacha上のRole権限が必要です。

## 設定

`ralph init`またはRepo-localインストールにより、利用先へ`.ralph/config.json`が作られます。`projectName`をWacha上のプロジェクト名に合わせます。ディレクトリ名と異なる場合は初期化時に指定できます。

```bash
ralph init --project-name wacha-project-name
```

Wacha MCPのURLは`.ralph/config.json`の`wacha.url`を正本とし、`ralph init`を再実行すると`.mcp.json`へ反映されます。APMやRuntime設定を後から配置した場合も、最後に`ralph init`を再実行してください。

Claude Codeへ渡すAuthorizationヘッダーは`Bearer ${WACHA_AGENT_NAME}`です。`ralph run`がロールごとのAgent名を環境変数として設定してからClaude Codeを起動します。

自律実行でClaude Codeの権限確認を省略する必要がある場合だけ、内容を理解したうえで次を設定してください。

```json
{
  "claude": {
    "dangerouslySkipPermissions": true
  }
}
```

Codexを使う場合は`ralph init --agent-provider codex`で初期化するか、既存設定の`agentProvider`を`codex`へ変更します。既定では`workspace-write` Sandboxで実行します。

```json
{
  "agentProvider": "codex",
  "codex": {
    "command": "codex",
    "dangerouslyBypassApprovalsAndSandbox": false
  }
}
```

WorkerにGitコミットを含む完全な自律実行を許可する場合は、リポジトリを信頼できることを確認して`dangerouslyBypassApprovalsAndSandbox`を`true`にします。この設定ではCodexの承認とSandboxが無効になります。Codex ProviderはWacha MCPのURLと`WACHA_AGENT_NAME`を実行時設定として渡すため、`.codex/config.toml`への追記は不要です。

`agentProvider`は起動方式、`claude.command`と`codex.command`は各CLIの実行ファイルです。`ralph init`は全Roleに選択した`agentProvider`を設定します。Role側の値がトップレベルより優先されるため、後からProviderを切り替える場合は対象Roleの値を変更してください。Roleの`command`は専用の実行ファイルやラッパーが必要な場合だけ指定し、選んだProviderの`command`より優先されます。`model`を省略または空文字にすると各CLIの既定モデルを使い、指定した場合はCLIの`--model`へそのまま渡します。

```json
{
  "agentProvider": "claude",
  "claude": {
    "command": "claude"
  },
  "codex": {
    "command": "codex"
  },
  "roles": {
    "worker": {
      "agentName": "worker-node-001",
      "agentProvider": "codex",
      "model": "gpt-5.6-terra"
    },
    "reviewer": {
      "agentName": "reviewer-node-001",
      "agentProvider": "claude",
      "model": "sonnet"
    },
    "manager": {
      "agentName": "manager-node-001",
      "agentProvider": "claude",
      "model": "sonnet"
    }
  }
}
```

## 実行

Global版は利用先リポジトリのルートまたは配下で実行します。Gitリポジトリの場合はルートを自動検出します。

```bash
ralph run
```

`ralph run`は1プロセスでManagerの最終受入、Reviewerのコードレビュー、Workerの差し戻し対応、Workerの次の開発をこの順に判断します。各Agentは従来どおり1回にTaskを最大1件だけ処理します。差し戻しが複数ある場合、Worker自身が対象を選びます。

実行対象のRoleを指定できます。複数指定時は、指定したRoleだけをManager → Reviewer → Workerの優先順で1つずつ実行します。指定順は優先順位に影響せず、重複指定は1つにまとめます。

```bash
ralph run worker
ralph run reviewer
ralph run manager
ralph run reviewer manager
```

### Principal別の並行実行

Principalの既定値は`roles.<role>.agentName`。`--principal`（`-p`）で、そのプロセスが選択した全RoleのPrincipalを上書きできます。Provider・model・quotaKeyなどの設定はRoleの設定を使います。対象PrincipalにはWachaの必要なRole Grantを付与してください。

```bash
ralph run worker -p worker-a
# 別の端末で同時起動
ralph run worker -p worker-b
ralph run reviewer manager
ralph resume worker -p worker-a
```

GitHub方式では、同じProject checkoutで別Principalの同じRole、および同じPrincipalの別Roleを並行実行できます。同じPrincipal・同じRoleの重複だけをOSのファイルロックで拒否します。複数Roleを指定したプロセスは起動時に全対象のロックを取得し、どれかが取得できなければ全て解放して起動を中止します。PIDは診断用で、ロックファイルの存在自体では稼働を判断しません。ロックファイルを手動削除しないでください。

Taskの競合はサーバーのClaimで防ぎます。Claim取得に負けた場合は別候補を試し、通常の待機へ戻ります。自己レビュー・自己受入は別の業務規則として維持します。このローカルロックは別Clone・別端末には共有されません。

GitHub方式のWorktreeは`.ralph/worktrees/<principal>/<role>`です。Principal中のパス区切り等はエンコードします。Task・Claimとの対応は実行記録で保持し、別Taskの未完了作業を上書きしません。GitHub方式以外はAgentが共有checkoutを直接操作するため、Project内で1プロセスに制限します。

### 運用状態と再開

Quota・Pause・進捗なし回数は`.ralph/runtime.json`の`schemaVersion`・`quotas`・`agents[principal][role]`に保存します。QuotaはProviderとquotaKey単位、PauseはPrincipalとRole単位です。短い読み取り・変更・置換だけを固定ファイル`.ralph/runtime.lock`のOSロックで保護し、他のPrincipalの状態を上書きしません。Quotaの期限は既存値より短縮しません。`resume`は対象のPauseと失敗回数を解除し、Quota・Claim・Worktree・他Principalの状態を保持します。

Repo-local版は次のように実行します。

```bash
./.ralph/runtime/bin/ralph run
```

Repo-local版も複数Roleを指定できます。

```bash
./.ralph/runtime/bin/ralph run worker
./.ralph/runtime/bin/ralph run reviewer
./.ralph/runtime/bin/ralph run manager
./.ralph/runtime/bin/ralph run reviewer manager
```

既定では対象Taskがない間、300秒ごとに再確認します。WorkerはWachaの`availableFor: work`、Reviewerは`availableFor: review`に該当するTaskがある場合だけ起動します。Managerは`availableFor: acceptance`のうち、Reviewerを通過した`wait_accept`だけを対象にします。`in_review`の直接受入やStory管理は行いません。自動実行でWorkerを選ぶ際は、利用可能な`rejected`があれば新規Taskより先に対応するようPromptで指示します。ローカル方式では対象一覧に`limit`は指定せず、個別のTaskはAgentが選びます。GitHub方式ではRalphが候補から1件をClaimして、TaskとClaimをAgentへ渡します。Claim中のTaskは対象外となり、Claim失効などによって再び利用可能になるまで待機します。

Agent ProviderのToken枯渇、利用量制限、その他の異常終了や、Wachaの一時的な通信失敗が起きてもRalphプロセスは終了しません。Claudeの`resets 11:20pm (Asia/Tokyo)`またはCodexの`try again at 5:39 PM`形式のReset時刻を取得できた場合は、その時刻の60秒後まで待機します。時刻を取得・解釈できない場合は1800秒、通常エラーは300秒待って再試行します。ローカル方式の進捗なし・通常エラーが`retry.maxNoProgressAttempts`（既定3）回続いた場合はRoleを停止します。GitHub方式でWorktreeに紐づく操作エラーはAgentが診断し、復旧上限・進展なし・明示的なblockedではTaskを保留します。`ralph resume <role>`で停止解除と保留Taskの再開を指示します。運用状態は再起動後も保持します。

引数なし、または複数Roleを指定した`ralph run`では、Token上限をProviderと`quotaKey`単位で共有し、その間も別Provider・別quotaKeyのRoleを確認します。たとえばManagerのCodexがToken上限に達しても、ReviewerのClaudeで処理できるTaskがあれば続行します。待機期限後は通常の優先順位でManagerを再確認します。Roleを1つだけ指定した実行ではプロセス全体が待機します。

待機時間は`.ralph/config.json`で変更できます。

```json
{
  "pollIntervalSeconds": 300,
  "retry": {
    "initialSeconds": 300,
    "tokenLimitSeconds": 1800
  }
}
```

`initialSeconds`は通常エラー、`tokenLimitSeconds`はToken上限メッセージからReset時刻を取得できなかった場合のフォールバック待機時間です。Ralphプロセス自体が終了・強制停止された場合の自動再起動は行わないため、常駐運転では必要に応じて`launchd`や`systemd`などのプロセス管理を併用してください。

現在の状態はProject内で`ralph status`を実行して確認します。`--principal <principal>`で対象を絞れます。Runnerの停止、Agent実行中、結果確認・反映中、再試行待ち、保留Taskを区別し、Task名・実行記録のパス・保留理由を表示します。ローカル記録を読むだけで、MCPへの接続や状態変更は行いません。

GitHub方式では、Agentが`blocked`と判定しClaimの解放が確認できた保留Taskについて、同じProjectの別Taskに保留時点より新しいAccepted更新があれば、所有PrincipalのAgentを自動再起動して前提を再評価します。別Taskの受入は前提解消の保証ではなく、Agentが現在の要件と保留理由を照合します。再び保留した場合は、その保留より新しいAccepted更新まで再起動しません。通信断で操作の成否が不明な保留・復旧診断中の保留・他Principalの保留は自動再開しません。外部設定や検証データだけを変更した場合は、`ralph resume <role> --principal <principal>`で再評価を指示します。

GitHub方式では、ローカルで保留したTaskを実行可能数から除外してRoleを選択します。保留中のManager Taskだけが残っていても、実行可能なReviewer・Workerを確認します。待機ログにはRoleごとの実行可能数と保留数を表示し、保留理由と記録パスは状態変更時に記録します。

Agentの実際の開始・終了と結果反映を別々に記録します。GitHub方式のAgent実行中は、Claimを更新する60秒間隔の監視でAgentのPID・経過秒数・Taskを表示します。これはプロセスの生存確認であり、作業の進捗や正常完了を保証するものではありません。

実行ログはコンソールへ表示しながら、Project共通の`.ralph/logs/ralph.YYYY-MM-DD.log`へ直接追記します。各行には日本時間の日時と`Role:Principal`が付きます。スケジューラーの待機行はRole選択前なのでPrincipalを付けません。各プロセスはstdout・stderrを収集し、コンソールの出力先は分けて保持します。複数プロセス間の厳密な出力順は保証しません。`.ralph/logs/`はGit管理対象外にしてください。

```text
[2026-10-16 10:00:00][worker:worker-a] Worker対象: todo=1 available=1
[2026-10-16 10:02:15][reviewer:reviewer-a] Reviewer対象: in_review=1
```

日付の切替はファイルのrenameを使いません。各書き込みの日時から直接保存先を選び、同じ日付には追記します。新規ファイルは所有者だけが読み書きできます。

`logging.backupCount`は過去の保持日数で、1以上の整数、既定20です。当日分と過去20日分を保持し、各プロセスの日付ごとの最初の出力時に保存期限を超えた日付ファイルだけを削除します。同時削除で既に存在しないファイルは正常扱いにします。無出力の日はファイルを作らず、停止中は保存期限の削除も実行しません。`github-state`・`verification`・Worktreeは削除対象ではありません。

保存先の基準名は設定で変更できます。Project rootからの相対パスまたは絶対パスを指定します。例えば`ralph.log`は`ralph.YYYY-MM-DD.log`、`custom.txt`は`custom.YYYY-MM-DD.txt`になります。

```json
{
  "logging": {
    "path": ".ralph/logs/ralph.log",
    "idleHeartbeatSeconds": 3600,
    "backupCount": 20
  }
}
```

Taskがない場合もWachaの確認は`pollIntervalSeconds`間隔で継続しますが、同じ待機状態は毎回出力しません。待機開始、Task状態の変化、エラーは即時に記録し、状態が変わらない間は`idleHeartbeatSeconds`間隔で生存確認を1行だけ記録します。

プロジェクト固有のPromptが必要な場合は、利用先にファイルを置き、ロール設定へプロジェクトルートからの相対パスを指定します。

```json
{
  "roles": {
    "worker": {
      "agentName": "worker-node-001",
      "prompt": ".ralph/prompts/worker.md"
    }
  }
}
```

GitHub方式の並行実行では、依存関係・DB・portなど検証用の書き込み先もWorktreeまたはClaim単位に分離してください。共通のDBや固定portを書き換えるsetup・検証コマンドは並行実行用になりません。

## 関連文書

- [Principal別の並行実行に関する設計判断](../docs/adr/0001-principal-role-parallel-execution.md)
- [実行状態形式の移行手順](../docs/operations/ralph-runtime-migration.md)

## 操作エラーの復旧

GitHub方式の操作エラーは、保存済み成果物と操作記録を引き継いだAgentが診断します。既定3回・15分の範囲で復旧し、進展のないTaskは保留して他のTaskを続行します。通信断で成否不明のPR・コメントを重複作成しません。設定・判断結果・保留からの再開は[GitHub運用](GITHUB.md#agentによる操作エラーの復旧)を参照してください。
