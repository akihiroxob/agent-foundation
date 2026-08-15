# Shared Knowledge

複数プロジェクトで再利用できる、恒久的な知識を管理します。

```text
knowledge/
  engineering/       一般的な開発原則、判断基準、失敗事例
  ralph/             Ralph Runnerとループ運用に関する知識
  proposals/
    open/             Workerが提起し、Reviewerの確認を待つ候補
    closed/           採用、不採用、保留の判断が完了した候補
```

明確な発火条件と繰り返し可能な手順を持つ知見は、Knowledgeではなく`.apm/skills/`へ昇格します。プロジェクト固有の仕様や設計判断は、利用先リポジトリのKnowledgeまたはADRへ残します。
