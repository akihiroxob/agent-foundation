---
name: verify-browser-ac
description: UIの受入条件を実ブラウザで検証し、操作・表示・権限差の根拠を残す。Playwright MCPで探索的に画面確認する際に使用する。
---

# Verify Browser AC

受入条件から操作シナリオを選び、実際の表示と操作結果を確認する。E2Eテストの作成は前提にしない。

## 検証環境

- Ralph Contextの`browserVerificationEnabled`がtrueなら、`ralph_verification`の`start_browser_preview`を引数なしで呼ぶ。返された`url`・`headSha`・`artifactDirectory`・`logPath`を記録し、対象の統合SHAと一致することを確認する。
- 起動するコードは対象worktree、ポートと一時データは実行専用。既存の開発Serverや運用DBを代わりに使わない。アプリ固有のログイン・データ準備はRepositoryの手順に従い、戻り値の`dataDirectory`内のテストデータを使う。
- Serverをsandbox内のシェルで起動し直したり、Agentからホストコマンド・環境変数・cwdを変更しない。起動失敗は`server.log`で切り分ける。

## 画面確認

1. Playwright MCPの`browser_navigate`で返されたURLを開く。`browser_snapshot`で画面と操作対象を確認してからクリック・入力する。
2. ACに必要な通常操作、空状態、入力エラー、権限差を検証する。報告済みの修正は同じ操作を再現し、更新・再読込・直接URLで結果が一致するか確認する。
3. レイアウトがACに含まれる場合は`browser_resize`でDesktopと狭い画面を確認し、`browser_take_screenshot`の画像を実際に読む。snapshotやDOMの存在だけで見た目の正常を判断しない。キーボード要件はTab・Enter・Escapeとfocus復帰を確認する。
4. 操作結果・期待結果・実際の結果と証跡pathを対応付ける。スクリーンショット等の`filename`は、返された`artifactDirectory`内の絶対pathを指定する。相対pathはMCPのcwdへ保存される場合があるため使わない。必要に応じてconsole・networkのエラーも確認する。Cookieや認証情報を報告に含めない。

## 終了と判定材料

- 成否にかかわらず`browser_close`と`stop_browser_preview`を呼ぶ。停止結果の`sourceUnchanged`と対象SHAを確認する。Serverの準備完了はAC成功を意味しない。
- Roleの判定に、確認したAC、操作、実際の結果、証跡path、未検証範囲を渡す。Worker/Reviewerの記録だけを独立検証の代わりにしない。
- MCP接続・ブラウザ起動・Server起動・ログイン等で必要な確認ができなければ、具体的な失敗箇所を示す。未確認のACを満たしたと扱わない。環境が変わらないまま同じ起動を繰り返さない。
