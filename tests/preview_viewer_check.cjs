// Exercise the shipped viewer logic without external browser dependencies.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const directory=process.argv[2],html=fs.readFileSync(path.join(directory,'index.html'),'utf8');
const raw=html.split('id="previewData">')[1].split('</script>')[0],elements=new Map();
const context=new Proxy({}, {get:(target,key)=>target[key]??(()=>{}),set:(target,key,value)=>(target[key]=value,true)});
class Element {
  constructor(id=''){this.id=id;this.children=[];this.style={};this.dataset={};this.handlers={};this.value='';this.checked=false;this._height=id==='detail'?310:110;this.width=800;this.textContent='';this.tagName='DIV';}
  append(...items){this.children.push(...items);}replaceChildren(...items){this.children=items;this.textContent='';}
  get height(){return this._height;}set height(value){this._height=value;}
  getAttribute(key){return key==='height'?this._height:null;}
  getBoundingClientRect(){return {left:0,width:800,height:this._height};}
  getContext(){return context;}
  addEventListener(name,callback){this.handlers[name]=callback;}
}
for(const id of html.matchAll(/\bid="([^"]+)"/g))elements.set(id[1],new Element(id[1]));
elements.get('previewData').textContent=raw;
const audio=elements.get('audio');audio.readyState=1;audio.paused=true;audio.currentTime=0;
audio.play=async()=>{audio.paused=false;};audio.pause=()=>{audio.paused=true;};
elements.get('follow').checked=true;
const window={devicePixelRatio:2,addEventListener:(name,callback)=>window[name]=callback};
const sandbox={console,window,document:{getElementById:id=>elements.get(id),createElement:()=>new Element(),addEventListener:()=>{}},requestAnimationFrame:()=>{}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(directory,'viewer.js'),'utf8'),sandbox);
const run=code=>vm.runInContext(code,sandbox);
const initialHeight=elements.get('detail').height;
run('render();render();render();');assert.equal(elements.get('detail').height,initialHeight,'DPR canvas height must stay stable');
run('seek(2000)');assert.equal(audio.currentTime,2);assert.match(elements.get('clock').textContent,/00:02/);
elements.get('detail').handlers.pointerdown({clientX:400});assert.equal(audio.currentTime,5);
elements.get('zoom').value='10000';elements.get('zoom').onchange();assert.equal(run('span'),10000);
elements.get('search').value='script';elements.get('search').oninput();assert.ok(elements.get('searchResults').children.length>0);
run('seek(1000)');elements.get('loopA').onclick();run('seek(3000)');elements.get('loopB').onclick();elements.get('loop').checked=true;elements.get('loop').onchange();audio.paused=false;audio.currentTime=4;run('tick()');assert.equal(audio.currentTime,1);audio.pause();
window.resize();assert.equal(elements.get('detail').height,initialHeight);
audio.handlers.seeked();assert.ok(elements.get('segment').textContent.length>0);
console.log('Viewer logic passed: seek, canvas DPR, zoom, search, loop, context and resize.');
