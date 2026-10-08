# GitHub Manager — Acceptance

RalphがAcceptance Claimとマージcommitの専用worktreeを用意済みです。
`get_role_instructions(manager, includeShared: true)`、`accept-task` Skill、Task・Storyの要件、コメント、Repositoryの指示を確認してください。

1. Contextの`expectedMergeSha`がcheckoutされていることを確認する。
2. ACを項目ごとに、統合されたコード・動作・必要な検証で確認する。Worker/Reviewerの報告だけを根拠にしない。
3. Contextの`resultPath`へJSONを出力する。満たしていれば`decision: accept`、不足があれば`decision: reject`。
   `summary`にはACごとの根拠、実施・未実施の検証、不足の場合は修正条件を記載する。

Gitの変更、GitHub操作、Claimの取得、Wachaのaccept/reject/releaseはRalphが担うため実行しない。
差し戻されたTaskは最新の統合branchを基点に、新しいPRで修正される。重大な回帰はrevertの要否を報告に記載する。
環境問題は`decision: blocked`と具体的な`summary`を出力する。JSON出力後、終了する。
