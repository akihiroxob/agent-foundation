# Ralph Runner

WachaのTask状態を監視し、Claude CodeまたはCodexのWorker・Reviewerを1 Taskずつ使い捨てで起動するRunnerです。

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

WorkerとReviewerで異なるAgent Providerを使う場合は、Roleごとの`agentProvider`へ`claude`または`codex`を指定します。Role側の指定がトップレベルの`agentProvider`より優先され、未指定の場合だけトップレベルへフォールバックします。Roleごとの`command`はProvider共通の`command`より優先されます。`model`を省略または空文字にすると各CLIの既定モデルを使い、指定した場合はCLIの`--model`へそのまま渡します。

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
      "command": "codex",
      "model": "gpt-5.6-terra"
    },
    "reviewer": {
      "agentName": "reviewer-node-001",
      "agentProvider": "claude",
      "command": "claude",
      "model": "sonnet"
    }
  }
}
```

## 実行

Global版は利用先リポジトリのルートまたは配下で実行します。Gitリポジトリの場合はルートを自動検出します。

```bash
ralph run worker
ralph run reviewer
```

Repo-local版は次のように実行します。

```bash
./.ralph/runtime/bin/ralph run worker
./.ralph/runtime/bin/ralph run reviewer
```

既定では対象Taskがない間、300秒ごとに再確認します。WorkerはWachaの`availableFor: work`、Reviewerは`availableFor: review`に該当するTaskがある場合だけ起動します。Claim中のTaskは対象外となり、Claim失効などによって再び利用可能になるまで待機します。

Agent ProviderのToken枯渇、利用量制限、その他の異常終了や、Wachaの一時的な通信失敗が起きてもRalphプロセスは終了しません。Token上限を検出した場合は1800秒、それ以外の失敗は常に300秒待って再試行します。Agentが終了コード0で終了してもTask状態が変化しなかった場合は、通常エラーと同じ待機になります。

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

`initialSeconds`は通常エラー、`tokenLimitSeconds`はToken上限検出時の固定待機時間です。Ralphプロセス自体が終了・強制停止された場合の自動再起動は行わないため、常駐運転では必要に応じて`launchd`や`systemd`などのプロセス管理を併用してください。

実行ログはコンソールへ表示しながら、プロジェクト共通の`.ralph/logs/ralph.log`へ追記します。各行には実行Role名が付きます。`.ralph/logs/`はGit管理対象外にしてください。

```text
[2026-09-20 10:00:00][worker] Worker対象: todo=1 available=1
[2026-09-20 10:02:15][reviewer] Reviewer対象: in_review=1
```

保存先は設定で変更できます。プロジェクトルートからの相対パスまたは絶対パスを指定します。

```json
{
  "logging": {
    "path": ".ralph/logs/ralph.log"
  }
}
```

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

WorkerとReviewerを同じworktreeで同時実行すると、Git操作や差分確認が競合します。並列実行する場合は役割ごとに別のGit worktreeを使用してください。
