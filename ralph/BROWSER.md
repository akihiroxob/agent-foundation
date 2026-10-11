# Managerの実ブラウザ検証

GitHub ManagerはPlaywright MCPと `verify-browser-ac` SkillでUIの受入条件を独立に確認する。E2Eテストの事前作成は不要。SkillはAPMで配布し、Ralphインストーラーにも同梱する。Projectの `.mcp.json` の定義は各Roleへ共通に配信し、Manager専用の証跡出力先とpreview接続を実行時に追加する。

## 設定

APMでProjectの `.mcp.json` に `playwright` のstdio定義を展開する。agent-foundationの `apm.yml` がパッケージ・バージョン・起動オプションの正本。`--isolated --headless` を必須とし、共有CDP・拡張機能・永続プロファイルは使用しない。

Projectの `.ralph/config.json` にホストで起動する検証用Serverを設定する。未設定または `browser: null` は無効。

```json
{
  "verification": {
    "hostCommands": [],
    "browser": {
      "command": ["npm", "start"],
      "env": {
        "PORT": "{port}",
        "APP_DB_PATH": "{dataDir}/preview.db"
      },
      "readyPath": "/"
    }
  }
}
```

`command`はshell文字列ではなく引数配列。`env`は検証用の値だけを明示する。`{port}`・`{dataDir}`は実行専用に割り当て、commandとenvの文字列で置換する。DB・認証・fixtureの設定名はProjectに合わせる。既存の開発DB・運用DBは指定しない。依存関係は既存の `github.setupCommands` で準備する。Serverは割り当てたポートのloopbackで待ち受けるよう設定する。`readyPath`は既定 `/`、起動待ちは120秒。HTTP 2xx/3xx/401/403で到達を確認する。

## Managerからの利用

Contextの `browserVerificationEnabled` がtrueなら、`browserVerificationSkill` を読む。ProjectのMCP定義にManager用の設定を重ねた `.ralph/mcp.json` を、共通アダプターが各CLIの形式へ変換して渡す。Provider内にPlaywright固有の分岐は持たない。MCPの管理方法は [MCP設定](MCP.md) を参照する。Playwright MCPはAgentのsandbox内のshellから起動しない。初回npm取得とブラウザ本体の準備はホスト側で必要。

`ralph_verification.start_browser_preview` を引数なしで呼び、返されたURLをPlaywrightで操作する。統合SHAと `headSha` を照合する。ログイン・テストデータ準備はRepositoryの手順を使う。正常・空状態・権限差・狭い画面・キーボード等をACに応じて確認する。見た目の確認はスクリーンショットを取得し、画像を読む。準備完了やHTTP成功だけでACを受け入れない。

証跡は `.ralph/verification/<claimId>/browser/` に保持する。Playwrightのoutput directoryも同じ場所に設定し、worktreeの掃除後も残す。`session-*/preview.json` に対象SHA・source fingerprint・Serverログpath・開始/終了時刻・停止理由を記録する。環境変数は最小限のOS設定と明示した検証用envだけであり、RalphのGitHub tokenやWacha Credentialを継承しない。

終了時は `playwright.browser_close` と `ralph_verification.stop_browser_preview` を呼び、`sourceUnchanged` を確認する。MCPのstdin切断・SIGTERM・SIGINTでもServerを停止する。Claim無効化・Server終了を60秒ごとに確認し、Serverの保持上限は1時間。通常の切断処理が動かない強制終了・端末停止後は、記録とプロセスを照合して残存Serverを復旧手順で確認する。Agent側の `--dangerously-bypass-approvals-and-sandbox` は不要。
