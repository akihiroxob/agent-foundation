# Ralph Runner

WachaのTask状態を監視し、Claude CodeのWorkerまたはReviewerを1 Taskずつ使い捨てで起動するRunnerです。

## 責務

- Ralph Runner: Taskの有無の確認、エージェントプロセスの起動、異常時の停止
- Wacha: Task、担当、進捗、レビュー状態の管理
- Agent provider: Claude Codeなどの実行環境固有の起動処理
- 利用先リポジトリ: プロジェクト固有の設定、仕様、知識

現在対応している組み合わせはWachaとClaude Codeです。

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
```

`ralph init`は次のRepo固有設定を作成または更新します。

```text
.ralph/config.json       RalphとWachaの設定
.mcp.json                Claude CodeのWacha MCP接続設定
.claude/settings.json    Wacha MCPの有効化とツール権限
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

既定では対象Taskがない間、300秒ごとに再確認します。Agent実行後もTask状態が変化しない場合は、無限にAgentを起動しないようエラー終了します。

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
