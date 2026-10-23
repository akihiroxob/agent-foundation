# MCP設定

追加MCPはProjectルートの `.mcp.json` の `mcpServers` で管理する。APMで展開した定義も同じ入口で読み込む。サーバー名を増やす際にProviderを修正する必要はない。

```json
{
  "mcpServers": {
    "custom-tool": {
      "command": "npx",
      "args": ["-y", "your-mcp-package"],
      "env": { "SERVICE_TOKEN": "${SERVICE_TOKEN}" }
    },
    "remote-tool": {
      "type": "http",
      "url": "https://example.test/mcp",
      "headers": { "Authorization": "Bearer ${SERVICE_TOKEN}" }
    }
  }
}
```

## 設定の責務

- WachaはRalphの実行管理に必須の接続。URLとRole別の認証は既存のRalph設定に従う。Project側の同名定義よりRalph設定を優先する。
- Projectの追加MCPは全Roleに共通して読み込む。
- GitHub実行では、共通処理がProjectルートの定義へWachaとClaim専用の `ralph_verification` を重ね、worktreeの `.ralph/mcp.json` に保存する。必要なホスト検証やpreviewを設定した場合だけ検証接続を追加する。
- Managerのブラウザ検証処理はPlaywrightの独立セッションを検証し、Claim別の証跡出力先を追加する。
- Providerは共通アダプターの結果をCLIへ渡す。CodexのWacha専用接続は維持する。

GitHub実行の設定元はProjectルートであり、worktree内の `.mcp.json` の変更で接続を差し替えない。元の `.mcp.json` は変更せず、生成した `.ralph/mcp.json` はGitへ登録しない。

GitHub連携を使わない実行では、起動ディレクトリの `.mcp.json` を共通アダプターで読み込む。未配置なら追加MCPなしで起動できる。壊れた定義や変換できない設定がある場合は、Agentを起動せず具体的なエラーを表示する。

## CLIへの受け渡し

`ralph/lib/mcp_config.py` が定義を検証・変換し、`ralph/lib/mcp_config.sh` が引数を配列として渡す。shellで設定値を評価しない。

- Codex: stdio・Streamable HTTPを `-c mcp_servers.<name>...` で渡す。MCP設定ファイルを直接読む機能の差をここで吸収する。
- Claude Code: `--mcp-config` と `--strict-mcp-config` で同じ定義を渡し、列挙したサーバーの `mcp__<name>` を許可する。同期的な検証の完了を取得するため、MCP自動バックグラウンド化を無効にする。

stdioの共通項目は `command`・`args`・`env`・`cwd`、HTTPの共通項目は `type: "http"`・`url`・`headers`。`type` の省略時はstdio。サーバー名は英数字・`_`・`-`を使う。Codexでは必須接続として起動を待ち、ツール実行を許可する。起動待ちは120秒、ツール待ちは既定3660秒。`timeout` を指定するとCodexのツール待ち時間としてミリ秒から変換する。

Codex向けstdioの環境変数参照は、`"SERVICE_TOKEN": "${SERVICE_TOKEN}"` のように同じ名前の変数をそのまま転送する形式を使う。HTTP認証は `Bearer ${VARIABLE}`、その他のHTTPヘッダーは `${VARIABLE}` を変数名参照へ変換する。これらの認証値をCLI引数へ展開しない。Codexでは変数の別名転送・文字列内への埋め込み・URL内の変数参照・旧SSE接続を変換しないため、明示的なエラーにする。Claude CodeのSSE定義はそのまま受け渡せる。

Codexの接続形式は [公式MCP仕様](https://developers.openai.com/codex/mcp) と [設定リファレンス](https://developers.openai.com/codex/config-reference) に従う。

接続設定の変更は次回のAgent起動で反映する。実行中のMCP接続を差し替えない。
