---
name: verify-browser-ac
description: UIの受入条件を実ブラウザで検証し、操作・表示・権限差の根拠を残す。Playwright MCPで探索的に画面確認する際に使用する。
---

# Verify Browser AC

受入条件から操作シナリオを選び、実際の表示と操作結果を確認する。E2Eテストの作成は前提にしない。

## 検証環境

- Ralph Contextの`browserVerificationEnabled`がtrueなら、`ralph_verification`の`start_browser_preview`を呼ぶ。通常は引数なしとし、カタログ等の別サーバーが必要ならtools/listに公開された許可済み`profile`を選ぶ。切替前に現在のpreviewを停止する。返された`url`・`headSha`・`artifactDirectory`・`logPath`を記録し、対象の統合SHAと一致することを確認する。
- 起動するコードは対象worktree、ポートと一時データは実行専用。既存の開発Serverや運用DBを代わりに使わない。アプリ固有のログイン・データ準備はRepositoryの手順に従い、戻り値の`dataDirectory`内のテストデータを使う。
- Serverをsandbox内のシェルで起動し直したり、Agentからホストコマンド・環境変数・cwdを変更しない。起動失敗は`server.log`で切り分ける。Contextの`verificationPreparation`が許可する準備ファイルは専用コピーで修正し、完了済み検証を止めてから再起動・再検証する。

## 検証データ

表示・参照に必要な状態はRepositoryで許可した準備手順を使い、専用環境へ用意する。プレビュー設定に準備コマンドがある場合は起動前に実行される。戻り値の `preparation` の終了コード・ログを確認し、作成したID・Role・シナリオを記録する。具体的なDB構造・準備コマンドはRepositoryの手順に従う。

専用環境のMCPで準備する際は、[use-mcp-tools](../use-mcp-tools/SKILL.md)に従い、接続先・主体・scope・現在のTool契約を照合する。準備manifestのTool一覧が観測情報か許可条件かをPolicyで確認する。

初期データが不足しても、それだけで検証を終了しない。準備ログ・manifestが示すRepositoryのPolicyを読み、専用プレビューで許可された公開API・操作・検証用主体を使って、必要な状態を追加してから確認する。実行中のTask/Claimを操作しないというRoleの制約と、明示的に許可された専用環境の検証用Entity操作を区別する。

- 操作前に返された専用URLのorigin・対象SHA・データ保存先を照合する。普段の業務MCPや既存開発環境へ検証データを作らない。
- 作成・変更したEntity ID、検証用主体、操作結果を証跡へ残す。公開操作がACなら準備時とは別に対象の権限で実操作を確認する。
- 時間経過の確認は、Policyで定めた検証用の有効期間と実際の期限を使う。更新・解放・時刻や応答の差し替えを、期限切れの証拠にしない。
- 必要な操作が許可範囲外、または対象SHAに入口がない場合に、不足する状態・試した操作・必要な追加手順を具体的に示す。検証対象の本体ソース、schema、運用データ、実行基盤の保護を緩めて補わない。許可済みの専用準備コードの実行不備は修正して続行し、修正理由と差分を残す。

管理操作・保存・認可がACの対象なら公開操作を実行して確認する。準備スクリプトの成功をその操作の検証の代わりにしない。先行機能が不足している場合はTask依存とACの割当を確認する。

## 画面確認

1. Playwright MCPの`browser_navigate`で返されたURLを開く。`browser_snapshot`で画面と操作対象を確認してからクリック・入力する。
2. ACに必要な通常操作、空状態、入力エラー、権限差を検証する。報告済みの修正は同じ操作を再現し、更新・再読込・直接URLで結果が一致するか確認する。
3. レイアウトがACに含まれる場合は`browser_resize`でDesktopと狭い画面を確認し、`browser_take_screenshot`の画像を実際に読む。snapshotやDOMの存在だけで見た目の正常を判断しない。キーボード要件はTab・Enter・Escapeとfocus復帰を確認する。
4. 操作結果・期待結果・実際の結果と証跡pathを対応付ける。スクリーンショット等の`filename`は、返された`artifactDirectory`内の絶対pathを指定する。相対pathはMCPのcwdへ保存される場合があるため使わない。必要に応じてconsole・networkのエラーも確認する。Cookieや認証情報を報告に含めない。

## 終了と判定材料

- 成否にかかわらず`browser_close`と`stop_browser_preview`を呼ぶ。停止結果の`sourceUnchanged`と対象SHAを確認する。専用準備がある場合は`preparationWorkspace`の取得元SHA・fingerprint・差分と`preparationUnchanged`を確認し、準備変更後の古い結果を流用しない。Serverの準備完了はAC成功を意味しない。
- Roleの判定に、確認したAC、操作、実際の結果、証跡path、未検証範囲を渡す。Worker/Reviewerの記録だけを独立検証の代わりにしない。
- MCP接続・ブラウザ起動・Server起動・ログイン等で必要な確認ができなければ、具体的な失敗箇所を示す。未確認のACを満たしたと扱わない。環境が変わらないまま同じ起動を繰り返さない。
