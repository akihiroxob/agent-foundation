# Ralph Runner

WachaのTask状態を監視し、Claude CodeまたはCodexのWorker・Reviewer・最終受入専用Managerを1 Taskずつ使い捨てで起動するRunnerです。

GitHub方式の設定・資格情報・worktree・PR・受入・復旧は [GitHub Workflow](GITHUB.md) を参照してください。

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
以前に作成した設定に`roles.manager`がない場合は、上記の設定例を参考に追加してください。自動実行には3つのRoleそれぞれの`agentName`と、Wacha上のRole権限が必要です。

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

`agentProvider`は起動方式、`claude.command`と`codex.command`は各CLIの実行ファイルです。`ralph init`は全Roleに選択した`agentProvider`を設定します。Role側の値がトップレベルより優先されるため、後からProviderを切り替える場合は対象Roleの値を変更してください。Roleの`command`は専用の実行ファイルやラッパーが必要な場合だけ指定し、選んだProviderの`command`より優先されます。`model`を省略または空文字にすると各CLIの既定モデルを使い、指定した場合はCLIの`--model`へそのまま渡します。以前に作成した設定でRoleの`command`が不要なら削除できます。

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

Roleごとに実行する場合は次を使います。

```bash
ralph run worker
ralph run reviewer
ralph run manager
```

Repo-local版は次のように実行します。

```bash
./.ralph/runtime/bin/ralph run
```

Roleごとに実行する場合は次を使います。

```bash
./.ralph/runtime/bin/ralph run worker
./.ralph/runtime/bin/ralph run reviewer
./.ralph/runtime/bin/ralph run manager
```

既定では対象Taskがない間、300秒ごとに再確認します。WorkerはWachaの`availableFor: work`、Reviewerは`availableFor: review`に該当するTaskがある場合だけ起動します。Managerは`availableFor: acceptance`のうち、Reviewerを通過した`wait_accept`だけを対象にします。`in_review`の直接受入やStory管理は行いません。自動実行でWorkerを選ぶ際は、利用可能な`rejected`があれば新規Taskより先に対応するようPromptで指示します。ローカル方式では対象一覧に`limit`は指定せず、個別のTaskはAgentが選びます。GitHub方式ではRalphが候補から1件をClaimして、TaskとClaimをAgentへ渡します。Claim中のTaskは対象外となり、Claim失効などによって再び利用可能になるまで待機します。

Agent ProviderのToken枯渇、利用量制限、その他の異常終了や、Wachaの一時的な通信失敗が起きてもRalphプロセスは終了しません。Claudeの`resets 11:20pm (Asia/Tokyo)`またはCodexの`try again at 5:39 PM`形式のReset時刻を取得できた場合は、その時刻の60秒後まで待機します。時刻を取得・解釈できない場合は1800秒、通常エラーは300秒待って再試行します。進捗なし・通常エラーが`retry.maxNoProgressAttempts`（既定3）回続いた場合はRoleを停止し、`ralph resume <role>`で再開します。停止状態は再起動後も保持します。

`ralph run`の自動実行では、Token上限をProviderと`quotaKey`単位で共有し、その間も別Provider・別quotaKeyのRoleを確認します。たとえばManagerのCodexがToken上限に達しても、ReviewerのClaudeで処理できるTaskがあれば続行します。待機期限後は通常の優先順位でManagerを再確認します。Roleを指定した実行では従来どおりプロセス全体が待機します。

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

実行ログはコンソールへ表示しながら、プロジェクト共通の`.ralph/logs/ralph.log`へ追記します。各行には実行Role名が付きます。`.ralph/logs/`はGit管理対象外にしてください。

```text
[2026-09-20 10:00:00][worker] Worker対象: todo=1 available=1
[2026-09-20 10:02:15][reviewer] Reviewer対象: in_review=1
```

保存先は設定で変更できます。プロジェクトルートからの相対パスまたは絶対パスを指定します。

```json
{
  "logging": {
    "path": ".ralph/logs/ralph.log",
    "idleHeartbeatSeconds": 3600
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

複数Roleを同じworktreeで同時実行すると、Git操作や差分確認が競合します。1つのcheckoutに複数のRunnerを起動しないでください。並列実行する場合は別checkout・別設定・別Agent名を使用してください。GitHub方式のTask実行はRunnerが専用worktreeを用意します。
