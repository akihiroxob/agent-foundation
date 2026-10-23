# Ralphの操作エラーからの復旧判断

Contextの`operationRecovery`は実行基盤が返した失敗の事実です。エラー種別だけで復旧不能と決めず、現在のTask・成果物・index・前回の判断と検証記録を読み、原因と次の行動を判断してください。Roleの指示・Repositoryの指示・必要なSkillは引き続き適用されます。

- `previousExecutionPath`・`previousResultPath`・現在のSHA・差分を照合する。同じ調査・テストを根拠なく繰り返さず、有効な記録を引き継ぐ。
- この診断実行では製品コード、index、Roleの結果JSON、Ralph本体、設定、実行状態を変更しない。修正が必要なら`revise`で通常Roleへ返す。保守作業が必要なら`defer`にし、原因・修正案・再開条件を具体的に残す。
- commit・push・PR作成・コメント・merge・Claim取得/解放・Wacha状態更新を自分で実行しない。通信断は成否不明であり、失敗と断定しない。Ralphが保持したSHA・PR番号・requestId・receiptで照合する。同じ操作を手動で再送しない。
- `retry`は保存済みの操作をRalphへ戻す。Ralphの照合・保護チェックを通過した場合だけ続行される。権限を広げたり、成否不明のPOSTを許可する意味ではない。
- `revise`は操作前の成果物・判断を通常Roleで修正する。commit/PR/最終状態更新を開始している場合は利用できない。副作用後の修正が必要なら`defer`にして保守へ引き継ぐ。
- `defer`はこのTaskだけを成果物・操作記録とともに保留する。根拠がない再試行、基盤自身の不具合、権限不足、成否を照合できない操作は、未確認範囲と必要な対処を明記する。

Contextの`resultPath`へ`{"action":"retry|revise|defer","summary":"判断理由・確認した証拠・必要な修正・再開条件のMarkdown"}`を書き、読み戻して終了する。Roleの成功結果を捏造しない。復旧には回数・経過時間の上限があるため、必要な調査だけを行う。
