---
name: recover-agent-execution
description: Agentの成果物作成後にGit・PR・実行結果の反映が失敗したとき、Worktree・結果・実行記録を照合し、成果物を保持して復旧する。停止原因の調査、復旧案の作成、承認済みの再開に使用する。
---

# Recover Agent Execution

成果物と実行記録を保持し、失敗した段階から再開する。調査依頼だけなら原因と復旧案までを扱い、再開・push・PR操作・業務状態の変更はユーザーから与えられた権限と作業範囲に従う。

## 実行状態の照合

- 対象のProject・Role・Task・Runnerを特定する。書き込みを伴う復旧前に対象Runnerを停止し、同じWorktreeを操作するAgentがいないことを確認する。他の実行を一括停止しない。
- 停止時刻付近のログ、pause理由、実行記録、結果JSONを確認し、Agent実行・検証・結果解析・commit・push・PR・業務状態の反映のどこで止まったかを判別する。
- 実行記録からWorktree・branch・HEAD・Claim・PRを特定する。復旧操作前に実行記録・結果JSON・必要な差分をリポジトリ外へ退避する。未追跡の成果物も確認し、認証情報を報告へ出力しない。
- Worktreeが登録された対象と一致することを確認する。作業差分とindexの差分を別々に調べ、結果JSONの成果物一覧と照合する。リネームは旧パスの削除と新パスの追加の両方を確認する。

```bash
git worktree list --porcelain
git -C /path/to/worktree status --short
git -C /path/to/worktree rev-parse HEAD
git -C /path/to/worktree diff --name-status --no-renames
git -C /path/to/worktree diff --cached --name-status --no-renames
git -C /path/to/worktree ls-files --others --exclude-standard
```

- commit・push・PR・業務操作は応答が途絶しても成功していることがある。保存済みSHA・PR番号・requestId・成功receiptと、利用可能な正規APIで確認した現在状態を照合する。状態を確認できない場合は成功・未実施のどちらにも決めつけず、変更を保持して停止する。
- Claimの所有者・期限、Taskの現在段階、要件、PR headを確認する。他Agentの有効Claim、段階・要件・headの変更があれば、保存済み判断をそのまま反映しない。

## 復旧の選択

- 結果JSONと成果物が有効で、停止原因が反映処理にある場合は、反映処理または実行環境を修正し、保存済み実行をRunnerの復旧機構へ戻す。実装やPRを最初から作り直さない。
- 結果JSONが欠落・不正、または検証未完了なら、同じWorktreeで作業を引き継ぐ。最終回答から成功結果を捏造せず、必要な検証を完了して結果JSONを書き、読み戻す。
- 認証・依存関係・sandboxが原因なら、その原因だけを解消する。停止していた検証を実行し、対象コードと検証対象の一致を確認する。
- 期限切れClaimはRunnerの再取得機構を使用する。単純な期限切れを理由に実行記録を削除したり、別Taskへ成果物を付け替えたりしない。
- 継続条件を満たさない場合は、差分と実行記録を保持して理由を報告する。実行の破棄・Claim解放・再選択は、影響を確認してその操作が承認されている場合だけ行う。

## Ralphでの適用

Project rootの次の記録を確認する。ローカル方式ではGitHub用実行記録がないことがあるため、方式を先に確認する。

- `.ralph/logs/ralph.log`または設定されたログパス
- `.ralph/pauses/<role>.json`
- `.ralph/github-state/<role>.json`の`cwd`・`resultPath`・`pendingResult`・`executionError`・`commitSha`・`prNumber`・`finalCommand`
- `.ralph/github-state/completed/`の成功・掃除記録
- `.ralph/verification/`の検証記録とログ

原因を解消し、再開が承認されていればProject rootで実行する。

```bash
ralph resume worker
ralph run
```

`worker`は対象Roleへ置き換える。`resume`は停止状態の解除であり、反映成功ではない。Runnerがすでに動いている場合は二重に`run`しない。保存済み実行はRunnerに引き継がせ、結果の再処理・Claim再取得・成功receiptの照合を手作業で置き換えない。

`--discard-execution`はClaim解放と実行の退避・再選択を伴うため、通常の再開には付けない。`reset --hard`・`clean`・Worktreeの強制削除・手動の完了状態書き換えでエラーを隠さない。詳細は利用中のRalphの`ralph/GITHUB.md`と`ralph/lib/github_workflow.py`、CLIの`resume`実装を参照する。

## 復旧結果の確認

停止箇所の後続ログと現在状態を確認し、成功した処理・残る問題・保持した成果物を報告する。commit SHA・PR・業務状態が期待どおりであること、重複操作がないことを確認する。再開指示を出しただけなら、復旧完了とは報告しない。
