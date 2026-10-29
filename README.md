# agent-harness

Codex と Claude Code に共通のエージェント設定を配布するための APM パッケージ雛形です。

## 方針

- Skills・Instructions・Hooks・MCP依存は APM で共通管理する
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

## ブラウザ操作用MCP

`apm.yml` の `dependencies.mcp` にPlaywright MCPを定義しています。`npx`で固定バージョン `0.0.83` を起動するため、Playwrightのグローバルインストールは不要です。Node.js 18以上とnpm（npx）、利用するブラウザ本体が必要です。初回起動時にはnpmパッケージの取得が発生します。

起動オプションは `--isolated --headless` です。各MCPプロセスが独立した一時プロファイルを使用し、ブラウザ画面を表示せず動作します。ログイン状態は終了時に破棄されます。UIの見た目を確認するときは、Agentがスクリーンショットも取得してください。

利用先ではAPMによるパッケージ導入時にMCP定義も展開します。依存パッケージの自己定義MCPに対してAPMが信頼確認を要求する場合は、利用先の `apm.yml` に同じPlaywright定義を直接記載して `apm install` を実行します。既定のChromeが導入されていない環境では、Playwright MCPの `browser_install` ツールでブラウザを準備できます。

このMCPはブラウザ操作を提供します。RalphはProjectの `.mcp.json` に展開された追加MCPを、共通アダプター経由で各Providerへ渡します。Wachaの接続はRalphが管理し、GitHub Managerのブラウザ検証ではPlaywrightの証跡出力先を実行時に追加します。MCPの管理方法は [MCP設定](ralph/MCP.md) を参照してください。検証対象アプリのServerはProjectで `verification.browser` を設定した場合に、専用worktree・ポート・一時データでホスト側から起動できます。設定方法と終了管理は [Managerの実ブラウザ検証](ralph/BROWSER.md) を参照してください。

参照: [APMのMCP導入](https://microsoft.github.io/apm/consumer/install-mcp-servers/)、[Playwright MCP](https://github.com/microsoft/playwright-mcp)。

## 同梱Skill

- `investigate-bug`: 不具合を再現・切り分けし、最小限の修正と検証を行う
- [`recover-agent-execution`](.apm/skills/recover-agent-execution/SKILL.md): Git・PR・結果反映で停止したAgent実行を調査し、成果物を保持して復旧する
- `review-documentation-impact`: 実装変更のドキュメント影響を判定し、Workerによる更新とReviewerによる整合性確認を行う
- [`use-mcp-tools`](.apm/skills/use-mcp-tools/SKILL.md): MCPの現在のTool契約とProjectの許可範囲を照合し、操作・検証データ準備・失敗の切り分けを行う
- [`verify-browser-ac`](.apm/skills/verify-browser-ac/SKILL.md): Playwright MCPでUIの受入条件を独立に確認し、操作結果・画像・未検証範囲を残す

## Ralph Runner

WachaのTaskを監視し、Claude CodeまたはCodexのWorker、Reviewer、最終受入専用Managerを使い捨てで起動するRunnerを同梱しています。

```bash
python3 scripts/install_ralph.py --global
cd /path/to/project
ralph init
ralph run
```

Runnerを利用先に固定する場合は`python3 scripts/install_ralph.py --target /path/to/project`を使用できます。詳細は[`ralph/README.md`](ralph/README.md)を参照してください。利用先の`.ralph/config.json`は再インストール時にも保持されます。

## カスタマイズ

最初に変更する場所は次の3点です。

1. `policy/runtime-policy.json` の許可・確認・拒否コマンド
2. `.apm/instructions/base.instructions.md` の共通方針
3. `.apm/skills/` の共通Skill

プロジェクト固有の技術・ドメインルールは、このパッケージではなく利用先リポジトリ側へ置くことを推奨します。

## GitHubでのTask実行

PR作成、Reviewとmerge、merge commit上のAC受入、worktree隔離、再試行と復旧は [Ralph GitHub Workflow](ralph/GITHUB.md) を参照してください。
