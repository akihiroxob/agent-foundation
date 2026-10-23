# Architecture Decision Records

このリポジトリ自身の設計判断を記録します。利用先プロジェクトの設計判断は、それぞれのリポジトリのADRへ残します。

新しいADRは[`TEMPLATE.md`](TEMPLATE.md)を複製し、`NNNN-short-title.md`の形式で追加します。

## 決定一覧

- [0001: PrincipalとRoleを単位としてRalphを並行実行する](0001-principal-role-parallel-execution.md)
- [0002: 操作エラーの復旧判断をAgentへ委譲する](0002-agent-operation-recovery.md)
