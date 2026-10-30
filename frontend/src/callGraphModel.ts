export type TraceEvent={id:number;run_id:string;kind:string;role:string;title:string;payload:Record<string,any>;created:string};
export type Invocation={id:string;start:TraceEvent;events:TraceEvent[];ended:boolean;failed:boolean;models:number;parentId?:string};
export type GraphNode={id:string;name:string;shortName:string;hint:string;kind:string;x:number;y:number;width:number;height:number;invocations:Invocation[];models:number;status:'returned'|'running'|'interrupted'|'error'};
export type GraphEdge={id:string;from:string;to:string;kind:'route'|'nested'|'return'|'sequence';evidence:number[];count:number};
export type CallGraphModel={runId:string;nodes:GraphNode[];edges:GraphEdge[];events:TraceEvent[];invocations:Invocation[];width:number;height:number};
const roles:Record<string,{name:string;hint:string;x:number;y:number}>={
 'Intent Structurer':{name:'意图整理',hint:'H1 · SIRA',x:190,y:12},
 'Payment Main Supervisor':{name:'总协调',hint:'任务分派',x:190,y:110},
 'Payment Action Generator':{name:'购物规划',hint:'查询与方案',x:28,y:208},
 'Payment Evaluation Supervisor':{name:'评审协调',hint:'分派与汇总',x:352,y:208},
 'Request/Intent Consistency Evaluator':{name:'需求一致性评审',hint:'用户意图',x:28,y:318},
 'Environment/Feasibility Evaluator':{name:'可行性评审',hint:'当前环境',x:190,y:318},
 'Transaction/Outcome Evaluator':{name:'交易结果评审',hint:'候选与回执',x:352,y:318},
 'Payment Revisor':{name:'方案整理',hint:'修订与交回',x:190,y:428},
 'H2':{name:'交易核验',hint:'H2 · TIAA',x:28,y:428},
 'H3':{name:'动态调整',hint:'H3 · DBDA',x:352,y:428}
};
const keyOf=(e:TraceEvent)=>String(e.payload.role_key||e.role);
const returnsToCoordinator=(from:string,to:string)=>
 to==='Payment Main Supervisor'&&['Payment Action Generator','Payment Evaluation Supervisor','Payment Revisor'].includes(from)||
 to==='Payment Evaluation Supervisor'&&['Request/Intent Consistency Evaluator','Environment/Feasibility Evaluator','Transaction/Outcome Evaluator'].includes(from);
/** Layout only. Roles and edges must be justified by this run's recorded evidence. */
export function buildCallGraph(input:TraceEvent[],active:boolean):CallGraphModel{
 const sorted=[...new Map(input.map(e=>[`${e.run_id}:${e.id}`,e])).values()].sort((a,b)=>a.id-b.id);
 const runId=sorted.at(-1)?.run_id||'',events=sorted.filter(e=>e.run_id===runId);
 const starts=[...new Map(events.filter(e=>e.kind==='agent_start'&&typeof e.payload?.node_id==='string').map(e=>[e.payload.node_id,e])).values()];
 const grouped=new Map<string,TraceEvent[]>();
 for(const e of events){const id=e.payload?.invocation_id;if(typeof id==='string'){const own=grouped.get(id)||[];own.push(e);grouped.set(id,own);}}
 const invocations:Invocation[]=starts.map(start=>{const own=grouped.get(start.payload.node_id)||[],end=own.find(e=>e.kind==='agent_end');return {id:start.payload.node_id,start,events:own,ended:!!end,failed:end?.payload.outcome==='error',models:own.filter(e=>e.kind==='model_end').length,parentId:start.payload.parent_node_id||undefined};});
 const byId=new Map(invocations.map(n=>[n.id,n])),nodes:GraphNode[]=[],nodeMap=new Map<string,GraphNode>();
 for(const invocation of invocations){const id=keyOf(invocation.start);let node=nodeMap.get(id);
  if(!node){const unknown=nodes.filter(n=>!roles[n.id]).length,spec=roles[id]||{name:invocation.start.role,hint:'其他已观测角色',x:28+(unknown%3)*162,y:762+Math.floor(unknown/3)*130};
   node={id,name:invocation.start.role,shortName:spec.name,hint:spec.hint,kind:invocation.start.payload.node_kind||'agent',x:spec.x,y:spec.y,width:140,height:68,invocations:[],models:0,status:'returned'};nodes.push(node);nodeMap.set(id,node);}
  node.invocations.push(invocation);node.models+=invocation.models;if(node.kind!==invocation.start.payload.node_kind)node.kind='mixed';
 }
 for(const n of nodes)n.status=n.invocations.some(i=>!i.ended)?(active?'running':'interrupted'):n.invocations.some(i=>i.failed)?'error':'returned';
 const edgeMap=new Map<string,GraphEdge>();
 function add(from:string,to:string,kind:GraphEdge['kind'],eventId:number){if(from===to||!nodeMap.has(from)||!nodeMap.has(to))return;const id=`${from}|${to}|${kind}`;let e=edgeMap.get(id);if(!e){e={id,from,to,kind,evidence:[],count:0};edgeMap.set(id,e);}if(!e.evidence.includes(eventId)){e.evidence.push(eventId);e.count++;}}
 const dispatched=new Set<string>();
 for(const e of events.filter(e=>e.kind==='route')){const owner=byId.get(e.payload.invocation_id),target=invocations.find(n=>n.start.id>e.id&&!n.parentId);
  // A dispatch request alone does not prove the target executed.
  if(owner&&target&&keyOf(owner.start)===e.payload.from&&keyOf(target.start)===e.payload.to){const a=keyOf(owner.start),b=keyOf(target.start);add(a,b,returnsToCoordinator(a,b)?'return':'route',e.id);dispatched.add(target.id);}
 }
 const rootOf=(n:Invocation)=>{const seen=new Set<string>();while(n.parentId&&byId.has(n.parentId)&&!seen.has(n.id)){seen.add(n.id);n=byId.get(n.parentId)!;}return n;};
 for(const n of invocations){if(n.parentId){const parent=byId.get(n.parentId);if(parent)add(keyOf(parent.start),keyOf(n.start),'nested',n.start.id);continue;}if(dispatched.has(n.id))continue;
  const previous=byId.get(n.start.payload.from_node_id);if(previous){const a=keyOf(rootOf(previous).start),b=keyOf(n.start);add(a,b,returnsToCoordinator(a,b)?'return':'sequence',n.start.id);}
 }
 return {runId,nodes,edges:[...edgeMap.values()],events,invocations,width:520,height:Math.max(200,...nodes.map(n=>n.y+n.height+68))};
}
export function edgePath(a:GraphNode,b:GraphNode,kind:GraphEdge['kind']):string{
 const ax=a.x+a.width/2,ay=a.y+a.height/2,bx=b.x+b.width/2,by=b.y+b.height/2;
 if(a.y===b.y){const y=a.y+a.height+34;return `M ${ax} ${a.y+a.height} C ${ax} ${y}, ${ax} ${y}, ${ax+(bx-ax)/2} ${y} S ${bx} ${y}, ${bx} ${b.y+b.height}`;}
 if(kind==='return'){const lower=a.y+a.height+15,upper=b.y-15;return `M ${ax} ${a.y+a.height} V ${lower} H 510 V ${upper} H ${bx} V ${b.y}`;}
 if(b.y<a.y){const lower=a.y+a.height+15;return `M ${ax} ${a.y+a.height} V ${lower} H 10 V ${by} H ${b.x}`;}
 if(b.y-a.y>200){const rail=bx>330?504:341;return `M ${a.x+a.width} ${ay} H ${rail} V ${by} H ${b.x+b.width}`;}
 const y1=a.y+a.height,y2=b.y,mid=(y1+y2)/2;return `M ${ax} ${y1} C ${ax} ${mid}, ${bx} ${mid}, ${bx} ${y2}`;
}
