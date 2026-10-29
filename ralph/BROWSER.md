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

`ralph_verification.start_browser_preview` を通常は引数なしで呼び、返されたURLをPlaywrightで操作する。統合SHAと `headSha` を照合する。ログイン・テストデータ準備はRepositoryの手順を使う。正常・空状態・権限差・狭い画面・キーボード等をACに応じて確認する。見た目の確認はスクリーンショットを取得し、画像を読む。準備完了やHTTP成功だけでACを受け入れない。

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

## 用途別のプレビュー

`verification.browser.profiles`に名前付きの起動設定を登録できる。各設定は`command`・`env`・`readyPath`・`setupCommands`を持ち、通常のpreviewと同じ制約を受ける。Agentはtools/listの`profile`候補から選び、`start_browser_preview({"profile":"catalog"})`で起動する。コマンド・環境変数・cwdの上書きはできない。切替前に`stop_browser_preview`を呼ぶ。製品画面・コンポーネントカタログ・build済みカタログを別設定にできる。

Claim失効でホスト検証が止まった場合は進捗を保持してblocked結果を返す。RalphがClaimを再取得し、同じworktreeと証跡を引き継ぐ`verificationRetry` ContextでAgentを再起動する。復旧には既存の回数・時間・進捗制限を適用する。

## MCPのTool発見と利用

`use-mcp-tools`はAPMとRalph runtimeに同梱する。GitHub実行Contextの`mcpUsageSkill`、ブラウザ検証Skillから参照できる。対象サーバーの現在のTool契約を読み、ProjectのPolicyと検証用主体のGrant・scopeに従って必要な操作を選ぶ。Toolが公開されているだけでは操作の許可にならない。

Projectは専用URL・データ保存先・検証用主体・Project/Workspace・対象Entity・目的で操作範囲を定義できる。その範囲の公開操作を許可するPolicyでは、manifestにあるTool名の観測一覧を追加の許可リストにしない。個別Tool名が許可条件となっているPolicyは明示的に変更されるまで守る。Project固有の操作名や業務規則は共通Skillへ取り込まない。

この手順はホストコマンドの許可配列やサービスの認可を変更しない。MCP操作はAgentが判断し、Ralphは接続ContextとSkillを提供する。

## 検証準備の編集境界

Reviewer/Managerは、Project設定の`verification.preparationFiles`に指定したRepository相対ファイルの専用コピーを修正できる。未設定は編集不可。対象本体のSHAは固定したまま、準備コードの取得元を`verification.preparationRevision`の完全commit SHAで指定できる。省略時は本体と同じSHAを使う。ファイルはそのcommitのGit blobから取得するため、固定Project checkoutの古いコピーを使わない。必要なローカルimport先とPolicy文書も準備ファイルに含める。

```json
{
  "verification": {
    "preparationFiles": ["scripts/prepare-preview.mjs", "docs/preview-policy.md"],
    "preparationRevision": "0123456789abcdef0123456789abcdef01234567"
  }
}
```

`verificationPreparation` Contextに専用directory・編集可能files・本体`headSha`・準備元`sourceSha`を返す。専用領域は`.ralph/verification/<worktreeClaimId>/preparation/`。Agentはこのコピーだけを変更する。本体、AC、期待結果、業務認可、Ralphの実行記録・ホスト許可コマンドは変更しない。

登録済みホストコマンドとプレビューのcommand/setupCommandsで、宣言済み準備ファイルを直接指定する引数（Project rootまたはWorktreeの絶対path、Repository相対path）は専用コピーへ置き換える。元の許可配列との完全一致判定は維持する。npm script内や文字列内の参照は書き換えないため、準備ファイルを直接引数に指定する。

変更した内容は実行記録の外側に版ごと保存する。検証記録には本体SHA・取得元SHA・準備fingerprint・差分path・`preparationUnchanged`を残す。prepare/host/previewでsymlink・範囲外ファイル・取得元変更を拒否する。準備を変更した実行のapprove/acceptには、同じClaim・本体SHA・現在の準備版で成功したホスト検証または正常停止したpreviewの証跡が必要。どのACが満たされたかはAgentが判断する。Reject/保留でも準備差分を保存し、失敗を成功に変換しない。

準備コードの変更で本体の不具合や不一致を隠さず、影響する検証を再実行する。恒久化する準備修正は保存した差分から通常のTask/PRへ戻す。専用コピーはWorktree清掃後も証跡とともに保持する。

Codex/Claude Codeには専用準備directoryだけを追加作業領域として渡す。sandbox全体を無効化したり、制御記録・証跡directory全体へ書込みを広げたりしない。
