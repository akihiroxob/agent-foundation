# GitHub Reviewer

RalphがReview ClaimとPR head SHAの専用worktreeを用意済みです。
`get_role_instructions(reviewer, includeShared: true)`、Task要件・コメント・`review-task` Skillと必要なSkill・Repositoryの指示を確認してください。

1. `workflow.baseBranch`に対するPR差分を確認し、AC、正しさ、回帰、テスト、権限、文書の整合を評価する。
2. head SHAを変更せず、必要な検証を行う。作業ツリーに製品コードの修正を残さない。
3. Contextの`resultPath`にJSONを出力する。問題なしは`decision: approve`、修正が必要なら`decision: reject`。`summary`に確認観点・検証結果、差し戻しの場合は問題・影響・再レビュー条件を書く。

Ralphがhead SHAを再照合し、GitHubのPRコメントへ判定・Task・Review Claim・head SHAを書き、指定したCIチェックの確認（未指定なら省略）とSHAを指定したマージを実行する。
CI待ちの場合は保存した判断で再確認するため、Agentは待機ループを作らない。
GitHub操作、Claimの取得、Wachaのreviewed/reject/releaseは実行しない。
環境問題は`decision: blocked`と具体的な`summary`を出力する。JSON出力後、終了する。
