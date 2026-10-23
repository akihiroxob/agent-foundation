# 0001: PrincipalとRoleを単位としてRalphを並行実行する

- Status: accepted
- Date: 2026-10-16

## Context

Project全体の起動ロックとRole単位の実行記録では、独立したPrincipalのWorkerを同じcheckoutで並行実行できなかった。TaskのClaimと専用Worktreeによって業務操作・作業場所を分離できるため、Project全体の排他は必要以上に実行を制限していた。

共有ログに各プロセスのTimedRotatingFileHandlerを適用すると、同じ退避先の削除・renameが競合し、ログを失う可能性がある。Quota・Pauseも複数プロセスがJSON全体を読み書きすると、他の更新を失う。

## Decision

- 同じcheckoutのPrincipal × Role単位でOSの起動ロックを取得する。別Principalの同じRole、および同じPrincipalの別Roleは並行起動できる。
- Task所有権はサーバーのClaim、作業場所はWorktreeで分離する。Gitの共有管理情報はGit自身のロックと限定的な再試行を使い、Git操作全体を覆う独自ロックを追加しない。
- Quota・Pause・進捗なし回数は1つのruntime.jsonに保持する。短い更新処理だけ固定ファイルのOSロックと原子的な置換で保護する。
- ログはイベントの日付に対応するファイルへ直接追記する。renameによるローテーションを使わず、保存期限を超えたファイルだけを削除する。
- GitHubが確認したマージ競合は保存済みの判断と共にReviewerへ返す。Reviewerが差し戻しを判断し、Workerが修正する。

## Consequences

RoleとPrincipalを追加するために新しい実行主体のモデルは必要ない。共有状態の更新は短時間だけ直列化され、Agentの実行中にはそのロックを保持しない。

ローカルのOSロックは別Clone・別端末には共有されない。共有checkoutを直接編集するGitHub方式以外の実行はProject内で1プロセスに制限する。検証用DB・port・依存関係など、Project固有の書き込み先も並行実行に適した分離が必要である。

操作と復旧の正本は[Ralphの実行ガイド](../../ralph/README.md)と[GitHub Workflow](../../ralph/GITHUB.md)とする。
