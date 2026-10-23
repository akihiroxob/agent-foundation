from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from scripts.install_ralph import install_global


class DeferredSchedulerTest(unittest.TestCase):
    def test_deferred_manager_does_not_starve_reviewer_and_is_not_logged_as_started(self):
        self.run_case(['reviewer', 'manager'], True)

    def test_only_deferred_tasks_log_the_reason_without_starting_an_agent(self):
        self.run_case(['manager'], False)

    def test_auto_idle_with_only_deferred_manager_keeps_runner_alive(self):
        self.run_case(['reviewer', 'manager'], False)

    def test_resumed_saved_execution_runs_even_when_no_new_task_is_claimable(self):
        self.run_case(['manager'], True, resume=True)

    def run_case(self, roles, expect_reviewer, resume=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root/'sample'
            project.mkdir()
            binaries=root/'bin';binaries.mkdir()
            curl=binaries/'curl'
            curl.write_text('''#!/usr/bin/env bash
if [[ "$*" == *list_projects* ]]; then
 printf '%s\\n' '{"result":{"structuredContent":{"projects":[{"id":"p","name":"sample"}]}}}'
elif [[ "$*" == *acceptance* ]]; then
 printf '%s\\n' '{"result":{"structuredContent":{"summary":{"byStatus":{"wait_accept":1}},"tasks":[{"id":"blocked","status":"wait_accept"}]}}}'
else
 printf '%s\\n' '{"result":{"structuredContent":{"summary":{"byStatus":{"in_review":1}},"tasks":[{"id":"review","status":"in_review"}]}}}'
fi
''')
            if not expect_reviewer:
                curl.write_text(curl.read_text().replace('"in_review":1', '"in_review":0').replace('[{"id":"review","status":"in_review"}]', '[]'))
            if resume:
                curl.write_text(curl.read_text().replace('[{"id":"blocked","status":"wait_accept"}]', '[]'))
            provider=binaries/'claude'
            provider.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$WACHA_AGENT_NAME" >>"$RALPH_TEST_DIR/runs"\n')
            curl.chmod(0o755);provider.chmod(0o755)
            env={**os.environ,'PATH':f'{binaries}:{os.environ["PATH"]}','RALPH_TEST_DIR':directory}
            launcher,runtime=install_global(root/'share',root/'launchers')
            subprocess.run([str(launcher),'init'],cwd=project,env=env,check=True,capture_output=True)
            config_path=project/'.ralph/config.json';config=json.loads(config_path.read_text())
            config['github']['enabled']=True;config['pollIntervalSeconds']=1;config['retry']['initialSeconds']=1
            config_path.write_text(json.dumps(config))
            (project/'.ralph/mcp.json').write_text('{"mcpServers":{}}')
            deferred=project/'.ralph/github-state/manager-node-001/deferred/manager-c.json'
            deferred.parent.mkdir(parents=True)
            deferred.write_text(json.dumps({'deferred':True,'role':'manager','principal':'manager-node-001','projectId':'p',
                'task':{'id':'blocked','title':'Blocked acceptance'},'deferredResumeGeneration':0,'deferredReason':'### 不足・修正条件\n\n- 前提Taskの受入待ちです。'}))
            if resume:
                from ralph.lib.runtime_state import operate
                operate(project, 'manager-node-001', 'manager', 'resume')
            (runtime/'lib/github_workflow.py').write_text("import json,os,sys\nfrom pathlib import Path\nroot=Path(os.environ['RALPH_TEST_DIR'])\n"
                "if sys.argv[1]=='prepare': print(json.dumps({'cwd':str(root/'sample'),'pending':False,'task':{'id':'review','title':'Ready review'}}))\n"
                "elif sys.argv[1]=='finish': (root/'done').touch()\n")
            process=subprocess.Popen([str(launcher),'run',*roles],cwd=project,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            log=project/'.ralph/logs'
            try:
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    if expect_reviewer and (root/'done').exists():break
                    if not expect_reviewer and any('前提Taskの受入待ち' in p.read_text() for p in log.glob('ralph.*.log')):break
                    time.sleep(.05)
            finally:
                process.terminate();stdout,stderr=process.communicate(timeout=5)
            if expect_reviewer:
                self.assertTrue((root/'done').exists(),stdout+stderr)
                self.assertEqual((root/'runs').read_text().splitlines()[0], 'manager-node-001' if resume else 'reviewer-node-001')
                self.assertIn('Agent実行開始',stdout)
                self.assertNotIn('managerを起動します',stdout)
            else:
                self.assertFalse((root/'runs').exists(),stdout+stderr)
                self.assertIn('available=0 保留=1' if len(roles)==1 else 'manager:実行可能=0,保留=1',stdout)
                self.assertNotIn('invalid choice',stdout+stderr)
                self.assertIn('前提Taskの受入待ち',stdout)
                self.assertIn(str(deferred),stdout)
                self.assertNotIn('Agent実行開始',stdout)
