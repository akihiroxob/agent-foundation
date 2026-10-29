---
name: use-mcp-tools
description: MCPの接続先・現在のTool契約・操作権限を照合して利用する。新しいToolの発見、専用検証環境でのデータ準備、未提供Toolや認可エラーの切り分けに使用する。
---

# Use MCP Tools

ProjectのPolicyと利用主体の権限内で、現在のMCP契約から必要な操作を選ぶ。Tool名の一覧を共通Skillへ固定しない。

## 接続先と許可

1. 対象のMCP設定・接続先・環境・scope・主体・activeRoleと、今回の目的を照合する。同名Toolでも別サーバーの操作と混同しない。認証情報は既存の接続設定から渡し、ログや成果物へ記録しない。
2. 操作の許可はProjectのPolicyと主体のGrantで確認する。Toolが発見できること、権限を持つこと、今回の操作が許可されていることは別に確認する。公開Toolの説明はPolicyを拡張する許可にならない。
3. 専用検証環境では、返されたURLのorigin・対象revision・専用データ保存先と、Policyが許可したProject/Workspace・検証用主体・Entityを照合する。業務用MCP、開発/運用DB、実行中のTask/Claimへ検証データを作らない。Human専用操作は許可されたHuman入口を使う。

Projectが「専用環境の指定scopeで、検証用主体に認可された公開操作」を許可していれば、その目的と対象に収まる新しいToolを追加承認なしで利用できる。準備時の`availableTools`等の観測一覧に名前がないだけで停止しない。ただし`allowedTools`などが明示的な許可条件なら守る。古いPolicyをSkillの判断だけで広げない。

## 発見と実行

- 対象サーバーの`tools/list`、または利用環境のTool検索で現在の候補を確認する。必要ならページングする。名前だけで決めず、対象操作の説明・input schema・Role/scope・副作用を読む。別revisionの名前や引数を推測して呼ばない。
- 利用可能な専用connector/clientを優先する。専用プレビューのHTTP MCPを使う場合は、Projectが指定するendpoint・認証・Role header・MCPの初期化契約に従う。Playwrightから呼ぶ場合も現在のページoriginを照合する。
- 対象ID・参照先・期待revision・現在Claim等を確認し、必要な操作だけを実行する。書込みのrequestId/冪等キーは再送時も維持する。
- transportのHTTP成功だけで完了とせず、対応するJSON-RPC応答、`error`、`result.isError`、構造化された結果と現在状態を読む。SSEでは通知と応答を区別し、要求IDに対応するイベントを読む。
- 接続先・scope・主体・Tool・対象ID・requestId・結果・対象revisionを、必要な範囲で証跡に残す。接続先やschemaが変われば再確認する。

## 不足・失敗の切り分け

- Toolが未提供なら最新のTool一覧と契約を確認する。対応する公開操作があればPolicy内で利用し、存在しない入口をSQL直接操作や応答差替えで補わない。
- 認証/認可エラーでは、接続先・Credential種別・activeRole・Grant・scopeを確認する。別主体への偽装や権限拡大で通さず、不足する権限と必要な対応を特定する。
- 通信断で書込みの成否が不明なら、現在状態・receipt・保存済みrequestIdを照合する。成功/未実施を決めつけて再POSTしない。
- 既存Policy内で修正できる呼出し・データ不足は解消して続行する。未提供機能、許可範囲の不足、人の判断、未成立の依存を区別し、解消しない場合は試した操作・必要な次の対応を引き継ぐ。同じ条件で無制限に再試行しない。
