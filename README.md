# agent-harness

Codex と Claude Code に共通のエージェント設定を配布するための APM パッケージ雛形です。

## 方針

- Skills・Instructions・Hooks は APM のプリミティブとして共通管理する
- Runtime policy は `policy/runtime-policy.json` を正本とする
- Codex Rules / sandbox 設定と Claude Code permissions はアダプターで生成する
- Ralph Runner はエージェントの外側でTask監視とプロセス起動を担当する
- 生成結果は利用先リポジトリへコピーしてレビュー可能にする

## 構成

```text
.apm/                       APM が配布する共通プリミティブ
  instructions/             コーディング方針
  skills/                   再利用可能な作業手順
  hooks/                    APM が各ハーネス形式へ変換する Hooks
policy/runtime-policy.json  共通ランタイムポリシー
adapters/                   Codex / Claude Code 出力ロジック
ralph/                      Ralph Runner本体、Backend、Provider、Prompt
knowledge/                  複数プロジェクトで再利用する恒久的な知識
docs/adr/                   このリポジトリ自身の設計判断
scripts/                    生成・検証スクリプト
tests/                      アダプターとRunnerのテスト
```

## セットアップ

```bash
curl -sSL https://aka.ms/apm-unix | sh
apm compile --validate
python3 scripts/generate_runtime_config.py
python3 -m unittest discover -s tests -v
```

## 生成結果

`python3 scripts/generate_runtime_config.py` を実行すると、`dist/` に次を生成します。

```text
dist/
  codex/.codex/config.toml
  codex/.codex/rules/default.rules
  claude/.claude/settings.json
```

利用先リポジトリでは、APMでSkills・Instructions・Hooksを導入した後、必要に応じて上記Runtime設定を配置します。

## 利用先リポジトリでの導入例

```bash
apm install akihiroxob/agent-harness#v0.1.0 --target codex,claude,agent-skills
```

Runtime設定は、当面はこのリポジトリをcloneして生成した`dist/`からコピーします。将来的にはAPM lifecycle scriptや専用CLIで一括導入できます。

## Ralph Runner

WachaのTaskを監視し、Claude CodeのWorkerまたはReviewerを使い捨てで起動するRunnerを同梱しています。

```bash
python3 scripts/install_ralph.py --target /path/to/project
cd /path/to/project
./.ralph/runtime/bin/ralph-loop worker
./.ralph/runtime/bin/ralph-loop reviewer
```

詳細は[`ralph/README.md`](ralph/README.md)を参照してください。利用先の`.ralph/config.json`は再インストール時にも保持されます。

## カスタマイズ

最初に変更する場所は次の3点です。

1. `policy/runtime-policy.json` の許可・確認・拒否コマンド
2. `.apm/instructions/base.instructions.md` の共通方針
3. `.apm/skills/` の共通Skill

プロジェクト固有の技術・ドメインルールは、このパッケージではなく利用先リポジトリ側へ置くことを推奨します。
