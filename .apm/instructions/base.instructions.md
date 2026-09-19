---
description: このハーネスを利用するリポジトリ向けの共通開発ルール。
applyTo: "**/*"
---

# 共通開発ルール

- 編集前に関連コードと既存の規約を確認する。
- 要求を満たす最小限の変更を優先する。
- タスクで明示されない限り、公開 API を維持する。
- 変更後は、リポジトリで利用する formatter、linter、型チェック、テストを実行する。
- 明示的な許可なく、本番依存関係の追加、データベーススキーマ変更、デプロイ、push を行わない。
- シークレット、`.env`、秘密鍵、認証情報を出力に含めない。
- 繰り返し作業を自動化できる場合は、再利用可能な Skill またはスクリプトを提案・作成する。

## 検証プロセスの所有権

- Agentが停止してよいのは、自身が検証のために起動したプロセスだけとする。人、他のAgent、外部のプロセス管理機構が起動したサービスは停止・再起動しない。
- 検証用プロセスは起動時の`$!`を保持し、終了時はそのPIDだけを指定する。
- プロセス名やコマンド文字列で一括終了する`pkill`、`killall`、`kill $(pgrep ...)`は使わない。
- 使用予定のportが既に使われている場合は既存プロセスを止めず、別portを使うか、共存要件との競合として報告する。

## ドキュメント整合性

- コード、設定、CLI、公開 API、利用者向け動作を変更した後は、関連ドキュメントへの影響を確認する。
- Worker はドキュメント更新が必要なら実装と同じ Task 内で更新し、不要ならその判断理由を作業報告へ残す。
- Reviewer は実装、設定例、README、API 文書、ADR、Knowledge の整合性を確認し、不一致を見逃したまま承認しない。
- 明確な発火条件と反復可能な手順は Skill、現在有効な原則・仕様・制約は Knowledge、プロジェクト固有の重要な設計判断と経緯は利用先リポジトリの ADR へ残す。
- 共通 Skill・Knowledge への昇格は通常のドキュメント修正と分け、Worker が候補を提起し、Reviewer が再利用性と根拠を確認する。

## フロントエンドアーキテクチャ

- UI を作成・変更する前に、要件を満たす既存の Component、style、実装パターンを検索する。適切なものは再利用し、重複する Component を作成しない。
- フロントエンドコードは Feature 単位で整理する。Feature 固有の UI、hook、API client、型、テストは、その Feature のディレクトリ内に置く。
- 共有 Component ディレクトリには、ドメインに依存しない汎用 UI のみを置く。route ファイルは Feature の組み立てに専念させ、ビジネスロジックを置かない。
- 他 Feature の内部モジュールを import しない。Feature の entry point（例: `features/tasks/index.ts`）から意図的な公開 API を提供し、それを経由して import する。
- JavaScript / TypeScript プロジェクトでは Biome を formatter と linter に使用する。フロントエンド変更後は、関連する型チェック・テストと併せてリポジトリの Biome チェックコマンドを実行する。

## バックエンドアーキテクチャ

- API・バックエンドの変更では、SOLID 原則を実用的に適用する。モジュールの責務を一つに保ち、統合境界では抽象に依存し、無関係な利用側に変更を強制しない。
- バックエンドコードはドメイン境界を中心にモデル化する。ドメインルールとドメイン型を HTTP framework、database client、その他の delivery・infrastructure の詳細から独立させる。
- request 処理、application use case、domain logic、infrastructure adapter を分離する。Controller は request の検証・変換を担い、Use Case は業務操作を統合し、Repository と外部 client は interface の背後に置く。
- Bounded Context ごとに model と rule の所有権を保つ。database entity や内部 domain model を Context 間で共有せず、明示的な contract を介して連携する。
- これらの境界を保つ最小限の設計を選ぶ。具体的なドメイン上または保守上の必要性なく、layer、interface、aggregate、pattern を導入しない。
