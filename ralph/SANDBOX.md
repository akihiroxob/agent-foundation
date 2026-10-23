# Sandboxの影響と現在の回避策

Agentが実行するshellと、ホスト側で実行するRalphのGit処理・検証MCPを区別する。以下は確認した障害と現在の実装上の制約であり、sandboxを無効にする手順ではない。

## 適用状態

Codex Providerは既定で `--sandbox workspace-write` を渡す。`codex.dangerouslyBypassApprovalsAndSandbox` がtrueのときだけbypassする。Claude Providerの `dangerouslySkipPermissions` はClaude側のpermission確認を省略する設定で、CodexのOS sandboxと同じものではない。

RoleごとにProviderと許可設定は異なる。各ProjectとRoleの実際の設定を確認し、すべてのAgentに同じ制約があると仮定しない。正本は各Projectの `.ralph/config.json` と適用されたProvider設定である。

## 影響箇所

| 箇所 | 影響・確認状況 | 現在の回避策 | 残る条件 |
| --- | --- | --- | --- |
| WorktreeのGit index・commit・push | Worktree外の共通Git管理領域への書き込みがAgentの許可範囲外になりうる。認証・ネットワークの制約も別途ある | Agentは実装と結果JSONを作成し、Ralphがホストでstage / check / commit / push / PR操作を行う。修正可能なcommit前エラーは同じWorkerへ返す | commit後の失敗は成功済み操作とreceiptを照合する。任意のGitエラーをWorker修正扱いにしない |
| 実HTTP / MCPの結合テスト | Codex sandbox内のloopback listenが`EPERM`となり、実HTTP等のテストがskipされた | Projectの `verification.hostCommands` に登録した引数配列を `run_host_verification` でホスト実行する | 未登録のコマンドは実行不可。skipと成功を区別し、終了コード・SHA・ログを確認する |
| UI検証用Server | Agent shell内でのサーバー起動がlisten制約を受ける | `verification.browser` の固定設定でホスト側から専用worktree・loopbackポート・一時DBのpreviewを起動する | Projectが起動コマンド・env・認証modeを正しく設定する。起動成功だけではAC成功にならない |
| preview DBへのfixture書き込み | `.ralph/verification/<claimId>/browser/.../data` が実行worktree外にあり、Agentの書き込み許可範囲外になる | `verification.browser.setupCommands` をホストで実行し、Ralphが割り当てた `{dataDir}` と明示envを渡す | Project固有の準備処理とシナリオが必要。通常操作で作れる状態は先行Taskと受入順を先に確認する |
| 親Repositoryの依存・build出力 | Worktreeに依存がないと親の`node_modules`や業務packageを参照し、親の`.vite-temp`への書き込みが`EPERM`になった。古いコードを検証した例もある | 対象worktreeで依存を準備し、ネイティブモジュールを含めそのworktreeへ解決されることを確認。自動準備が必要なら `github.setupCommands` に登録する | 親packageでの成功を対象SHAの証拠にしない。設定コマンドはProjectが管理する |
| ブラウザ・npmパッケージの初回準備 | MCPの起動主体、ネットワーク設定、ブラウザ本体の有無に依存する。ChromeのAbort全件がsandbox原因と確認されたわけではない | ProjectのMCP定義からホスト側の接続を用意し、ブラウザ検証手順に従う。初回取得・ブラウザ導入を事前に済ませる | CLI / MCPのstderr、パッケージ取得、ブラウザ起動を分けて調査する |
| Worktree外のソース・設定・global runtime更新 | この保守セッションの許可範囲外への書き込みには承認が必要。Agentが自分の実行許可を広げる操作とは分離する | ソース変更・検証後、許可されたホスト操作でインストールする。Projectの実行許可設定は管理側で保持する | 設定変更で書き込み・接続範囲を広げる場合は与えられた権限を確認する |

各ホストコマンドは実行コード自体のレビューも必要である。同じコマンド名でも中身が変われば別の副作用を持ちうる。許可コマンド一覧は汎用の隔離機構ではなく、登録したコードが既存の開発・運用データを扱わないことまで自動保証しない。

## Sandbox以外の原因

| 症状 | 原因・現在の扱い |
| --- | --- |
| `git diff --cached --check`の`new blank line at EOF` | ソース末尾の空行。sandboxによる失敗ではない。Ralphのcommit前チェックが検出し、上限付きでWorkerへ修正を返す |
| preview起動直後の`Transport closed`・`ERR_CONNECTION_REFUSED` | Node子プロセスが継承したMCP stdinを非ブロッキングへ変更し、Python側が入力終了と判断した問題を再現。preview・ホスト検証の子プロセスを`stdin=DEVNULL`にして修正済み |
| 前提TaskがRejectのまま後続Taskを受入できない | 計画・依存の問題。前提の修正・受入を優先し、後続は保留する。Skillの手順とRunnerの自動依存制御を混同しない |
| 検証データを作る公開入口が未実装 | 製品計画の不足または先行機能待ち。sandboxを外しても入口は増えない。順序・分割・ACの所有を確認し、必要な場合だけProjectのfixture手順を使う |
| OAuth期限切れ・PAT不足・SSH要求 | Provider認証とGit認証を区別して復旧する。sandboxの解除では認証情報を更新できない |

## 権限設定との区別

`policy/runtime-policy.json` と生成されるCodex / Claude設定には、コマンド許可や秘密ファイルの読み取りに関する方針もある。Agent固有のpermission・sandbox・ホストOSの制約を同一視しない。生成設定の適用とProviderの起動引数を確認する。Policyの各項目がすべてのProviderへ同じ強さで反映されるとは仮定しない。

ホスト側の検証は [GitHub Workflow](GITHUB.md)、preview・データ準備は [ブラウザ検証](BROWSER.md)、MCPの配信は [MCP設定](MCP.md) を参照する。
