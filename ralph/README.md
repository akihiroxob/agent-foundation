# Ralph Runner

WachaのTask状態を監視し、Claude CodeのWorkerまたはReviewerを1 Taskずつ使い捨てで起動するRunnerです。

## 責務

- Ralph Runner: Taskの有無の確認、エージェントプロセスの起動、異常時の停止
- Wacha: Task、担当、進捗、レビュー状態の管理
- Agent provider: Claude Codeなどの実行環境固有の起動処理
- 利用先リポジトリ: プロジェクト固有の設定、仕様、知識

現在対応している組み合わせはWachaとClaude Codeです。

## インストール

このリポジトリのルートで実行します。

```bash
python3 scripts/install_ralph.py --target /path/to/project
```

利用先には次のファイルが作成されます。

```text
.ralph/
  config.json
  runtime/
    bin/ralph-loop
    backends/wacha.sh
    providers/claude.sh
    prompts/
```

既存の`.ralph/config.json`は上書きしません。Runner本体を更新する場合は、同じコマンドを再実行します。

## 設定

`.ralph/config.json`の`projectName`をWacha上のプロジェクト名に合わせます。`projectRoot`を省略した場合、`.ralph/`の親ディレクトリが対象になります。

自律実行でClaude Codeの権限確認を省略する必要がある場合だけ、内容を理解したうえで次を設定してください。

```json
{
  "claude": {
    "dangerouslySkipPermissions": true
  }
}
```

## 実行

利用先リポジトリのルートで実行します。

```bash
./.ralph/runtime/bin/ralph-loop worker
./.ralph/runtime/bin/ralph-loop reviewer
```

既定では対象Taskがない間、300秒ごとに再確認します。Agent実行後もTask状態が変化しない場合は、無限にAgentを起動しないようエラー終了します。

プロジェクト固有のPromptが必要な場合は、利用先にファイルを置き、ロール設定へプロジェクトルートからの相対パスを指定します。

```json
{
  "roles": {
    "worker": {
      "agentName": "worker-node-001",
      "prompt": ".ralph/prompts/worker.md"
    }
  }
}
```

WorkerとReviewerを同じworktreeで同時実行すると、Git操作や差分確認が競合します。並列実行する場合は役割ごとに別のGit worktreeを使用してください。
