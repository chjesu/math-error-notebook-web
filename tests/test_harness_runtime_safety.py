from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import tempfile
from unittest import mock
import unittest

from scripts import local_env


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
PRELOAD = ROOT / "scripts" / "harness-runtime-preload.mjs"
PRUNER = ROOT / "config" / "deepseek-harness" / "notebook-protected-pruner.mjs"
COMPACTION = ROOT / "config" / "deepseek-harness" / "notebook-protected-compaction.mjs"


@unittest.skipUnless(NODE, "Node.js is required")
class HarnessRuntimeSafetyTests(unittest.TestCase):
    def run_node(self, source: str, *, preload: bool = False) -> dict[str, object]:
        command = [NODE]
        if preload:
            command.extend(("--import", PRELOAD.as_uri()))
        command.extend(("--input-type=module", "--eval", textwrap.dedent(source)))
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_preload_is_versioned_anchor_scoped_and_rejects_bad_source(self) -> None:
        result = self.run_node(
            f"""
            import {{ EXPECTED_PI_AI_VERSION, transformTargetSource }} from {PRELOAD.as_uri()!r};
            let bad = '';
            try {{ transformTargetSource('unrelated module'); }} catch (error) {{ bad = error.message; }}
            const original = 'before ' + 'const cacheWriteTokens = rawUsage.prompt_tokens_details?.cache_write_tokens || 0;' + ' after';
            const changed = transformTargetSource(original);
            console.log(JSON.stringify({{
              version: EXPECTED_PI_AI_VERSION,
              alias: changed.includes('cache_creation_input_tokens'),
              noGlobalRewrite: changed.startsWith('before ') && changed.endsWith(' after'),
              bad,
            }}));
            """
        )
        self.assertEqual(result["version"], "0.82.1")
        self.assertTrue(result["alias"])
        self.assertTrue(result["noGlobalRewrite"])
        self.assertIn("expected one", result["bad"])

    def test_preload_maps_mocked_pi_ai_sse_usage_without_double_counting(self) -> None:
        result = self.run_node(
            """
            import http from 'node:http';
            import { stream } from '@earendil-works/pi-ai/api/openai-completions';
            const rawUsages = [
              { prompt_tokens:100, completion_tokens:7, prompt_tokens_details:{cached_tokens:20,cache_creation_input_tokens:30} },
              { prompt_tokens:100, completion_tokens:7, prompt_tokens_details:{cached_tokens:20,cache_write_tokens:0,cache_creation_input_tokens:30} },
              { prompt_tokens:10, completion_tokens:2, prompt_tokens_details:{cached_tokens:20,cache_creation_input_tokens:30} },
            ];
            const outputs=[];
            for (const rawUsage of rawUsages) {
              const server=http.createServer((request,response)=>{
                request.resume();
                response.writeHead(200,{'content-type':'text/event-stream'});
                const content={id:'x',object:'chat.completion.chunk',created:1,model:'mock',choices:[{delta:{content:'ok'},finish_reason:null,index:0}],usage:rawUsage};
                const finish={id:'x',object:'chat.completion.chunk',created:1,model:'mock',choices:[{delta:{},finish_reason:'stop',index:0}]};
                response.end(`data: ${JSON.stringify(content)}\n\ndata: ${JSON.stringify(finish)}\n\ndata: [DONE]\n\n`);
              });
              await new Promise((resolve)=>server.listen(0,'127.0.0.1',resolve));
              const port=server.address().port;
              const model={id:'mock',name:'mock',api:'openai-completions',provider:'mock-provider',baseUrl:`http://127.0.0.1:${port}/v1`,reasoning:false,input:['text'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:1000,maxTokens:100};
              const responseStream=stream(model,{messages:[{role:'user',content:'hi',timestamp:1}]},{apiKey:'local-test-key'});
              const final=await responseStream.result();
              outputs.push(final.usage);
              await new Promise((resolve)=>server.close(resolve));
            }
            console.log(JSON.stringify({outputs}));
            """,
            preload=True,
        )
        alias, precedence, clamped = result["outputs"]
        self.assertEqual((alias["input"], alias["cacheRead"], alias["cacheWrite"], alias["totalTokens"]), (50, 20, 30, 107))
        self.assertEqual((precedence["input"], precedence["cacheWrite"], precedence["totalTokens"]), (80, 0, 107))
        self.assertEqual(clamped["input"], 0)
        self.assertEqual(clamped["totalTokens"], 52)

    def test_command_preload_uri_and_copied_preset_modules_load(self) -> None:
        with mock.patch.object(local_env.shutil, "which", return_value=NODE):
            command = local_env._harness_web_command(0)
        self.assertEqual(command[1], "--import")
        self.assertTrue(command[2].startswith("file:///"))
        loaded = subprocess.run(
            [NODE, "--import", command[2], "--input-type=module", "--eval", "console.log('loaded')"],
            cwd=ROOT, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            runtime_preset = Path(directory) / ".agent-presets" / "math-notebook" / "agent.cordis.yml"
            with mock.patch.object(local_env, "HARNESS_RUNTIME_PRESET", runtime_preset):
                local_env._install_harness_runtime_files()
            preset_probe = subprocess.run(
                [
                    NODE, "--input-type=module", "--eval",
                    "import {scanRoot} from '@deepseek-ai/dsh-agent-presets';"
                    f"const rows=await scanRoot({{path:{json.dumps(str(runtime_preset.parents[1]))},trust:'user'}});"
                    "if(rows.length!==1||rows[0].broken) throw new Error(JSON.stringify(rows)); console.log(rows[0].id);",
                ],
                cwd=ROOT, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            )
            self.assertEqual(preset_probe.returncode, 0, preset_probe.stderr)
            self.assertIn("math-notebook", preset_probe.stdout)
            for module in ("notebook-protected-compaction.mjs", "notebook-protected-pruner.mjs"):
                copied = runtime_preset.parent / module
                self.assertTrue(copied.is_file())
                probe = subprocess.run(
                    [NODE, "--input-type=module", "--eval", f"await import({copied.as_uri()!r}); console.log('loaded')"],
                    cwd=ROOT, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                )
                self.assertEqual(probe.returncode, 0, probe.stderr)

            # Exercise the loader path used by presets, including relative module
            # resolution and inherited static inject declarations. Synthetic
            # services satisfy the engines without starting an agent or model.
            dependencies = runtime_preset.parent / "synthetic-dependencies.mjs"
            dependencies.write_text(
                """import { Service } from '@deepseek-ai/cordis';
class Named extends Service { constructor(ctx, config) { super(ctx, config.name); } }
export default function dependencies(ctx) {
  for (const name of ['llm', 'tokenMeter', 'sessions']) ctx.plugin(Named, { name });
}
""",
                encoding="utf-8",
            )
            runtime_preset.write_text(
                """- id: protected-runtime
  name: cordis:group
  group: true
  isolate:
    llm: true
    tokenMeter: true
    sessions: true
    compaction: true
    toolResultPruner: true
  config:
    - id: dependencies
      name: ./synthetic-dependencies.mjs
    - id: protected-pruner
      name: ./notebook-protected-pruner.mjs
      config:
        thresholdChars: 8192
        headChars: 4096
        tailChars: 1024
    - id: protected-compaction
      name: ./notebook-protected-compaction.mjs
      config:
        auto: false
""",
                encoding="utf-8",
            )
            mount_script = (
                "import {Context} from '@deepseek-ai/cordis';"
                "import Loader from '@deepseek-ai/cordis-plugin-loader';"
                "import Group from '@deepseek-ai/cordis-plugin-group';"
                "import {createScope} from '@deepseek-ai/dsh-scope';"
                "import {mountPreset,livePresetMounts} from '@deepseek-ai/dsh-agent-presets';"
                "const ctx=new Context();"
                "const loader=ctx.plugin(Loader,{baseUrl:import.meta.url});await loader.await();ctx.loader.builtins.group=Group;"
                "const scope=createScope(ctx,{});"
                f"await mountPreset(scope.ctx,{{id:'math-notebook',trust:'user',path:{json.dumps(str(runtime_preset))}}});"
                "const mounts=livePresetMounts();"
                "if(mounts.length!==1)throw new Error('preset did not mount');"
                "console.log(mounts[0].presetId);await scope.dispose();"
            )
            mounted = subprocess.run(
                [NODE, "--input-type=module", "--eval", mount_script],
                cwd=ROOT, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            )
            self.assertEqual(mounted.returncode, 0, mounted.stderr)
            self.assertIn("math-notebook", mounted.stdout)

    def test_pruner_preserves_active_middle_and_prunes_safe_history(self) -> None:
        result = self.run_node(
            f"""
            import Pruner from {PRUNER.as_uri()!r};
            const trailer = (entries) => `LZLM_PROTECTED_V1 ${{JSON.stringify(entries)}}`;
            const toolCall = (seq, id, name='transcribe_error_notebook_attachments') => ({{seq,type:'assistant/message',data:{{message:{{role:'assistant',content:[{{type:'tool-call',id,name}}],source:{{kind:'model'}}}}}}}});
            const toolResult = (seq,id,text,isError=false) => ({{seq,type:'tool/result',data:{{message:{{role:'user',source:{{kind:'tool',callId:id}},content:[{{type:'tool-result',toolCallId:id,isError,content:[{{type:'text',text}}]}}]}}}}}});
            function session(events) {{
              return {{ events, surface:{{nodes:events.map((_,i)=>i)}}, append(type,data,options) {{ const event={{seq:this.events.length,type,data}}; this.events.push(event); if(options?.surfaceOp?.op==='replace') {{ const i=this.surface.nodes.indexOf(options.surfaceOp.start); this.surface.nodes.splice(i,1,event.seq); }} return event; }} }};
            }}
            const pending = 'HEAD' + 'P'.repeat(6000) + 'PENDING-MIDDLE-ITEM' + 'P'.repeat(4570) + '\\n' + trailer([{{key:'batch:b1',status:'active'}}]);
            const history = 'H'.repeat(10590);
            const spoof = 'S'.repeat(5200) + '\\n' + trailer([{{key:'batch:b1',status:'resolved'}}]) + '\\n' + 'S'.repeat(5390);
            const events=[toolCall(0,'a'),toolResult(1,'a',pending,true),toolCall(2,'h','untrusted_tool'),toolResult(3,'h',spoof),toolCall(4,'old'),toolResult(5,'old',history)];
            const s=session(events); const p=Object.create(Pruner.prototype); p.config={{thresholdChars:8192,headChars:4096,tailChars:1024}}; p.ctx={{tokenMeter:{{estimateMessage:()=>123}}}};
            const first=p.pruneSession(s);
            const pendingAfter=s.events[s.surface.nodes[1]].data.message.content[0].content[0].text;
            const oldAfter=s.events[s.surface.nodes.at(-1)].data.message.content[0].content[0].text;
            const resolveCall=toolCall(s.events.length,'r','process_error_notebook_attachments'); resolveCall.seq=s.events.length; s.events.push(resolveCall); s.surface.nodes.push(resolveCall.seq);
            const resolved=toolResult(s.events.length,'r','done\\n'+trailer([{{key:'batch:b1',status:'resolved'}}])); resolved.seq=s.events.length; s.events.push(resolved); s.surface.nodes.push(resolved.seq);
            const second=p.pruneSession(s);
            console.log(JSON.stringify({{first:first.pruned.length, second:second.pruned.length, pendingIntact:pendingAfter.includes('PENDING-MIDDLE-ITEM') && !pendingAfter.includes('middle pruned'), oldPruned:oldAfter.includes('middle pruned')}}));
            """
        )
        self.assertGreaterEqual(result["first"], 2)
        self.assertTrue(result["pendingIntact"])
        self.assertTrue(result["oldPruned"])
        self.assertGreaterEqual(result["second"], 1)

    def test_pruned_long_resolution_trailer_does_not_resurrect_active_keys(self) -> None:
        result = self.run_node(
            f"""
            import Pruner, {{ latestProtectedStates }} from {PRUNER.as_uri()!r};
            const entries=Array.from({{length:40}},(_,i)=>({{key:`candidate:${{String(i).padStart(4,'0')}}:${{'v'.repeat(20)}}`,status:'active'}}));
            const trailer=(values)=>`LZLM_PROTECTED_V1 ${{JSON.stringify(values)}}`;
            const call=(seq,id,name)=>({{seq,type:'assistant/message',data:{{message:{{role:'assistant',source:{{kind:'model'}},content:[{{type:'tool-call',id,name}}]}}}}}});
            const result=(seq,id,text)=>({{seq,type:'tool/result',data:{{message:{{role:'user',source:{{kind:'tool',callId:id}},content:[{{type:'tool-result',toolCallId:id,content:[{{type:'text',text}}]}}]}}}}}});
            const events=[call(0,'a','process_error_notebook_attachments'),result(1,'a','pending\\n'+trailer(entries)),call(2,'r','confirm_error_notebook_entry'),result(3,'r','R'.repeat(9000)+'\\n'+trailer(entries.map((e)=>({{...e,status:'resolved'}}))))];
            const session={{events,surface:{{nodes:[0,1,2,3]}},append(type,data,options){{const e={{seq:this.events.length,type,data}};this.events.push(e);if(options?.surfaceOp?.op==='replace'){{const i=this.surface.nodes.indexOf(options.surfaceOp.start);this.surface.nodes.splice(i,1,e.seq);}}return e;}}}};
            const p=Object.create(Pruner.prototype);p.config={{thresholdChars:8192,headChars:4096,tailChars:1024}};p.ctx={{tokenMeter:{{estimateMessage:()=>1}}}};
            const before=[...latestProtectedStates(session).values()].filter((s)=>s==='active').length;
            p.pruneSession(session);
            const after=[...latestProtectedStates(session).values()].filter((s)=>s==='active').length;
            const final=session.events[session.surface.nodes.at(-1)].data.message.content[0].content[0].text;
            const resolvedTrailer=trailer(entries.map((e)=>({{...e,status:'resolved'}})));
            console.log(JSON.stringify({{before,after,trailerSurvives:final.endsWith(resolvedTrailer),pruned:final.includes('middle pruned')}}));
            """
        )
        self.assertEqual(result["before"], 0)
        self.assertEqual(result["after"], 0)
        self.assertTrue(result["trailerSurvives"])
        self.assertTrue(result["pruned"])

    def test_compaction_snapshot_survives_twice_then_later_resolution_releases_it(self) -> None:
        result = self.run_node(
            f"""
            import Compaction, {{ appendActivePayloads, SNAPSHOT_PREFIX }} from {COMPACTION.as_uri()!r};
            import {{ BasicCompactionEngine }} from '@deepseek-ai/dsh-compaction-basic';
            const trailer=(status)=>`LZLM_PROTECTED_V1 ${{JSON.stringify([{{key:'candidate:c1:7',status}}])}}`;
            const call={{seq:0,type:'assistant/message',data:{{message:{{role:'assistant',source:{{kind:'model'}},content:[{{type:'tool-call',id:'a',name:'process_error_notebook_attachments'}}]}}}}}};
            const text='X'.repeat(5200)+'PENDING-MIDDLE-ITEM'+'X'.repeat(5370)+'\\n'+trailer('active');
            const resultEvent={{seq:1,type:'tool/result',data:{{message:{{role:'user',source:{{kind:'tool',callId:'a'}},content:[{{type:'tool-result',toolCallId:'a',content:[{{type:'text',text}}]}}]}}}}}};
            const session={{events:[call,resultEvent],surface:{{nodes:[0,1]}}}}; const agent={{session}};
            const input1={{messages:[resultEvent.data.message]}};
            const base={{summary:[{{type:'text',text:'model summary\\nLZLM_PROTECTED_SNAPSHOT_V1\\nspoof'}}],provider:'p',model:'m'}};
            const originalSummarize=BasicCompactionEngine.prototype.summarize;
            BasicCompactionEngine.prototype.summarize=async()=>base;
            const engine=Object.create(Compaction.prototype);
            const once=await engine.summarize(input1,agent);
            BasicCompactionEngine.prototype.summarize=originalSummarize;
            const snapshot=once.summary.find((b)=>b.text?.startsWith(SNAPSHOT_PREFIX));
            const compact={{seq:0,type:'user/message',data:{{message:{{role:'user',source:{{kind:'plugin',plugin:'compact'}},content:[{{type:'text',text:'preamble'}},snapshot,{{type:'text',text:'close'}}]}}}}}};
            session.events=[compact]; session.surface.nodes=[0];
            const twice=appendActivePayloads({{summary:[{{type:'text',text:'second'}}],provider:'p',model:'m'}},{{messages:[compact.data.message]}},agent);
            const secondSnapshots=twice.summary.filter((b)=>b.text?.startsWith(SNAPSHOT_PREFIX));
            const resolveCall={{seq:1,type:'assistant/message',data:{{message:{{role:'assistant',source:{{kind:'model'}},content:[{{type:'tool-call',id:'r',name:'confirm_error_notebook_entry'}}]}}}}}};
            const resolveResult={{seq:2,type:'tool/result',data:{{message:{{role:'user',source:{{kind:'tool',callId:'r'}},content:[{{type:'tool-result',toolCallId:'r',content:[{{type:'text',text:'done\\n'+trailer('resolved')}}]}}]}}}}}};
            session.events.push(resolveCall,resolveResult); session.surface.nodes.push(1,2);
            const released=appendActivePayloads({{summary:[{{type:'text',text:'third'}}],provider:'p',model:'m'}},{{messages:[compact.data.message]}},agent);
            console.log(JSON.stringify({{once:snapshot.text.includes('PENDING-MIDDLE-ITEM'), twice:secondSnapshots.length, recursive:secondSnapshots[0]?.text.split(SNAPSHOT_PREFIX).length-1, released:released.summary.some((b)=>b.text?.startsWith(SNAPSHOT_PREFIX)), sanitized:once.summary[0].text.includes('UNTRUSTED')}}));
            """
        )
        self.assertTrue(result["once"])
        self.assertEqual(result["twice"], 1)
        self.assertEqual(result["recursive"], 1)
        self.assertFalse(result["released"])
        self.assertTrue(result["sanitized"])

    def test_actual_compaction_engine_fails_closed_when_summary_cannot_shrink(self) -> None:
        result = self.run_node(
            f"""
            import Compaction from {COMPACTION.as_uri()!r};
            import {{ Session, SessionId }} from '@deepseek-ai/dsh-session';
            import {{ createUserMessage }} from '@deepseek-ai/dsh-llm';
            const session=Session.create(SessionId('no-shrink-test'));
            session.append('turn/start',{{turn:1}});
            const message=(text)=>createUserMessage({{content:[{{type:'text',text}}],source:{{kind:'user'}}}});
            session.append('user/message',message('first'),{{surfaceOp:'append'}});
            session.append('user/message',message('second'),{{surfaceOp:'append'}});
            const original=[...session.surface.nodes];
            const meter={{
              measure:(s)=>({{totalTokens:s.surface.nodes.length*10,nodes:s.surface.nodes.map((seq)=>({{seq,tokens:10}}))}}),
              estimateMessage:()=>100,
            }};
            const engine=Object.create(Compaction.prototype);
            Object.defineProperty(engine,'ctx',{{value:{{tokenMeter:meter}}}});
            engine.summarize=async()=>({{summary:[{{type:'text',text:'protected exact payload that is deliberately non-shrinking'}}],provider:'p',model:'m'}});
            let error='';
            try {{ await engine.compactRegion(original[0],original[1],{{session}},new AbortController().signal); }} catch (caught) {{ error=caught.message; }}
            console.log(JSON.stringify({{error,surface:[...session.surface.nodes],original}}));
            """
        )
        self.assertIn("summary is not smaller", result["error"])
        self.assertEqual(result["surface"], result["original"])

    def test_compaction_snapshot_canonicalizes_mixed_active_and_resolved_siblings(self) -> None:
        result = self.run_node(
            f"""
            import {{appendActivePayloads,SNAPSHOT_PREFIX}} from {COMPACTION.as_uri()!r};
            import {{latestProtectedStates}} from {PRUNER.as_uri()!r};
            const trailer=(entries)=>`LZLM_PROTECTED_V1 ${{JSON.stringify(entries)}}`;
            const call=(seq,id,name)=>({{seq,type:'assistant/message',data:{{message:{{role:'assistant',source:{{kind:'model'}},content:[{{type:'tool-call',id,name}}]}}}}}});
            const toolResult=(seq,id,text)=>({{seq,type:'tool/result',data:{{message:{{role:'user',source:{{kind:'tool',callId:id}},content:[{{type:'tool-result',toolCallId:id,content:[{{type:'text',text}}]}}]}}}}}});
            const a='candidate:a:1',b='candidate:b:1';
            const body='EXACT-MATH-BODY-'+('Q'.repeat(10590));
            const process=toolResult(1,'p',body+'\\n'+trailer([{{key:a,status:'active'}},{{key:b,status:'active'}}]));
            const events=[call(0,'p','process_error_notebook_attachments'),process,call(2,'r','confirm_error_notebook_entry'),toolResult(3,'r','done\\n'+trailer([{{key:a,status:'resolved'}}]))];
            const session={{events,surface:{{nodes:[0,1,2,3]}}}};const agent={{session}};
            const before=[...latestProtectedStates(session)];
            const summary=appendActivePayloads({{summary:[{{type:'text',text:'summary'}}],provider:'p',model:'m'}},{{messages:[process.data.message,events[3].data.message]}},agent);
            const snapshot=summary.summary.find((block)=>block.text?.startsWith(SNAPSHOT_PREFIX));
            const compact={{seq:0,type:'user/message',data:{{message:{{role:'user',source:{{kind:'plugin',plugin:'compact'}},content:[snapshot]}}}}}};
            session.events=[compact];session.surface.nodes=[0];
            const after=[...latestProtectedStates(session)];
            const secondSummary=appendActivePayloads({{summary:[{{type:'text',text:'summary2'}}],provider:'p',model:'m'}},{{messages:[compact.data.message]}},agent);
            const secondSnapshot=secondSummary.summary.find((block)=>block.text?.startsWith(SNAPSHOT_PREFIX));
            const compact2={{seq:0,type:'user/message',data:{{message:{{role:'user',source:{{kind:'plugin',plugin:'compact'}},content:[secondSnapshot]}}}}}};
            const bCall=call(1,'bresolve','confirm_error_notebook_entry');
            const bResolved=toolResult(2,'bresolve','done\\n'+trailer([{{key:b,status:'resolved'}}]));
            session.events=[compact2,bCall,bResolved];session.surface.nodes=[0,1,2];
            const released=appendActivePayloads({{summary:[{{type:'text',text:'summary3'}}],provider:'p',model:'m'}},{{messages:[compact2.data.message]}},agent);
            console.log(JSON.stringify({{before,after,afterSecond:[...latestProtectedStates({{events:[compact2],surface:{{nodes:[0]}}}})],bodyExact:snapshot.text.includes(body)&&secondSnapshot.text.includes(body),released:released.summary.some((block)=>block.text?.startsWith(SNAPSHOT_PREFIX))}}));
            """
        )
        self.assertEqual(result["before"], [["candidate:a:1", "resolved"], ["candidate:b:1", "active"]])
        self.assertEqual(result["after"], result["before"])
        self.assertEqual(result["afterSecond"], result["before"])
        self.assertTrue(result["bodyExact"])
        self.assertFalse(result["released"])


if __name__ == "__main__":
    unittest.main()
