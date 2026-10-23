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
      "setupCommands": [["npm", "run", "prepare:acceptance", "--", "--data-dir", "{dataDir}"]],
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

終了時は `playwright.browser_close` と `ralph_verification.stop_browser_preview` を呼び、`sourceUnchanged` を確認する。子プロセスのstdinは閉じ、MCPの通信入力を継承させない。MCPのstdin切断・SIGTERM・SIGINTでもServerを停止する。Claim無効化・Server終了を60秒ごとに確認し、Serverの保持上限は1時間。通常の切断処理が動かない強制終了・端末停止後は、記録とプロセスを照合して残存Serverを復旧手順で確認する。Agent側の `--dangerously-bypass-approvals-and-sandbox` は不要。

## 検証データの準備

`verification.browser.setupCommands` はProjectが許可した起動前の準備コマンド一覧。省略時は実行しない。各コマンドを対象worktreeで順に実行し、プレビューと同じ明示env・`{dataDir}`・`{port}`を使う。Agentからコマンド・引数・cwd・データ保存先を上書きできない。

準備スクリプトはProjectで管理し、専用ディレクトリにDB等を作る。具体的なApplication・schema・シナリオをRalphへ埋め込まない。作成したID・Role・シナリオを標準出力へ返し、認証情報は出力しない。Project設定が許可の正本で、各実行時の人への確認は不要。コマンド自体が一時環境だけを扱うよう実装する。Ralphは任意のスクリプトのアクセス先を自動判定するものではない。

各コマンドの上限は120秒。失敗・timeout・Claim無効化・ソース/HEAD変更ではServerを起動しない。Claimを確認し、子プロセスのstdinを分離して終了を待つ。戻り値と `preview.json` の `preparation` にコマンド・終了コード・`setup-N.log` の参照を残す。準備の成功はAC成功を意味しない。

例の `prepare:acceptance` はProjectで用意するscript名で、Ralphに同梱されたコマンドではない。

## Principal別の並行検証

検証MCPにはRalphが選択したPrincipalとClaimを渡す。Roleだけから別Principalの実行を参照しない。毎回、Principal別の実行記録・Claim・登録済みWorktreeを照合する。Worktreeは`.ralph/worktrees/<principal>/<role>`を使う。Playwrightはisolated context、portは実行ごと、データと証跡はClaim/sessionごとに分離する。Projectの検証・setupコマンド自体が共有DB・固定port・共有node_modulesへ書き込まないことも確認する。

## 検証データの不足への対応

初期データだけでACの状態が揃わない場合は、Managerが利用先RepositoryのPolicyに従い、専用プレビューの公開API等で検証データを追加する。実行Contextの業務Task/Claimへの操作禁止と、専用環境の検証用Entity操作を区別する。許可する主体・操作・接続先はProjectが定め、共通Skillへ製品固有のAPIやDB構造を記載しない。未許可のホスト実行や運用データの変更を許可するものではない。
