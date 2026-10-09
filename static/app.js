const fields=['pl_rate','ru_rate','repetitions','language_pause','repeat_pause','card_pause'];
const $=id=>document.getElementById(id);
const example='3 dni - 3 дня\npod wpływem narkotyków (naćpani) - под воздействием наркотиков\nszlaban - шлагбаум\njest otwarty - он открыт\notwiera się - открывается';
let timer,generation=0,currentJob=null,controller;
let lineFrame,validatedText=null,checking=false,jobBusy=false,textRevision=0;
function updateSubmit(){
  const ready=validatedText!==null&&validatedText===$('text').value&&!checking&&!jobBusy&&$('form').checkValidity();
  $('submit').disabled=!ready;
  $('submit').setAttribute('aria-disabled',String(!ready));
}
function invalidateText(){textRevision++;validatedText=null;updateSubmit();}
for(const field of fields)$(field).addEventListener('input',updateSubmit);
function scheduleLines(){cancelAnimationFrame(lineFrame);lineFrame=requestAnimationFrame(updateLines);}
function syncLineScroll(){ $('line-gutter').scrollTop=$('text').scrollTop; }
function updateLines(){
  const text=$('text'),gutter=$('line-gutter'),measure=$('text-measure');
  const style=getComputedStyle(text);
  measure.style.width=`${text.clientWidth}px`;
  for(const property of ['font','letterSpacing','paddingTop','paddingBottom','paddingLeft','paddingRight','tabSize','textIndent'])measure.style[property]=style[property];
  const lines=text.value.split('\n'),rows=document.createDocumentFragment();
  for(const line of lines){const row=document.createElement('div');row.textContent=line||'\u200b';rows.append(row);}
  measure.replaceChildren(rows);
  // Read layout once after all mirror rows have been inserted.
  const heights=Array.from(measure.children,row=>row.getBoundingClientRect().height);
  const numbers=document.createDocumentFragment();
  heights.forEach((height,index)=>{const number=document.createElement('div');number.className='line-number';number.textContent=index+1;number.style.height=`${height}px`;numbers.append(number);});
  gutter.replaceChildren(numbers);
  syncLineScroll();
}
$('text').addEventListener('scroll',syncLineScroll,{passive:true});
$('line-gutter').addEventListener('scroll',()=>{if($('text').scrollTop!==$('line-gutter').scrollTop)$('text').scrollTop=$('line-gutter').scrollTop;},{passive:true});
$('line-gutter').addEventListener('wheel',event=>{
  if(!event.deltaY)return;
  const text=$('text'),unit=event.deltaMode===1?parseFloat(getComputedStyle(text).lineHeight):event.deltaMode===2?text.clientHeight:1;
  const before=text.scrollTop;text.scrollTop+=event.deltaY*unit;
  if(text.scrollTop!==before){event.preventDefault();syncLineScroll();}
},{passive:false});
new ResizeObserver(scheduleLines).observe($('text'));
document.fonts?.ready.then(scheduleLines);
function count(){scheduleLines();const n=$('text').value.split('\n').filter(x=>x.trim()).length;$('count').textContent=`${n} / 500 строк`;}
function error(message){$('errors').textContent=message;$('errors').hidden=false;}
function setDownloads(links={}){
  for(const kind of ['mp4','mp3']){
    const link=$(kind),available=Boolean(links[kind]);
    link.setAttribute('aria-disabled',String(!available));
    if(available){link.href=links[kind];link.removeAttribute('tabindex');}
    else{link.removeAttribute('href');link.tabIndex=-1;}
  }
}
for(const kind of ['mp4','mp3'])$(kind).addEventListener('click',event=>{if($(kind).getAttribute('aria-disabled')==='true')event.preventDefault();});
function stopWatch(){generation++;clearTimeout(timer);controller?.abort();}
function normalization(data){
  if(typeof data.normalized_text==='string')$('text').value=data.normalized_text;
  count();$('normalized-summary').hidden=false;
  $('normalized-summary').textContent=`Распознано: ${data.valid_count} пар. ${data.invalid_count?`Требуют исправления: ${data.invalid_count} строк`:'Ошибок нет'}`;
  if(data.errors?.length)error(data.errors.map(e=>`${e.line?`Строка ${e.line}: `:''}${e.message}\n${e.text}`).join('\n\n'));
}
function details(data){
  const d=data.detail;
  if(d?.normalized_text!==undefined){normalization(d);return;}
  error(Array.isArray(d)?d.map(x=>`${x.loc?.at(-1)||'Настройка'}: ${x.msg}`).join('\n'):d||'Ошибка запроса');
}
$('text').addEventListener('input',()=>{invalidateText();count();$('normalized-summary').hidden=true;});
$('example').addEventListener('click',()=>{$('text').value=example;invalidateText();count();$('normalized-summary').hidden=true;});
$('normalize').addEventListener('click',async()=>{
  validatedText=null;checking=true;updateSubmit();
  const revision=textRevision,input=$('text').value;
  $('errors').hidden=true;$('normalize').disabled=true;
  try{
    const response=await fetch('/api/normalize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:input})});
    const data=await response.json();
    if(revision!==textRevision||input!==$('text').value)return;
    if(!response.ok)details(data);else{normalization(data);if(data.valid_count>0&&data.invalid_count===0&&!data.errors?.length)validatedText=$('text').value;}
  }catch(e){error('Не удалось проверить текст. Проверьте соединение.');}
  finally{checking=false;$('normalize').disabled=false;updateSubmit();}
});
async function watch(id,version){
  if(version!==generation)return;
  controller=new AbortController();
  try{
    const response=await fetch(`/api/jobs/${id}`,{signal:controller.signal});
    if(version!==generation)return;
    if(response.status===404){localStorage.removeItem('polski-job');jobBusy=false;updateSubmit();$('cancel').hidden=true;error('Задание больше не найдено. Создайте новое.');return;}
    if(!response.ok)throw new Error('Не удалось получить состояние задания.');
    const data=await response.json();if(version!==generation)return;
    $('result').hidden=false;$('stage').textContent=data.stage;
    $('progress-label').textContent=data.total?`${data.done} / ${data.total}`:'';
    if(data.total){$('progress').max=data.total;$('progress').value=data.done;}else $('progress').removeAttribute('value');
    const finished=['done','failed','cancelled'].includes(data.state);
    $('cancel').hidden=finished;jobBusy=!finished;updateSubmit();$('progress').hidden=finished;
    if(finished){
      setDownloads(data.downloads||{});
      $('metadata').textContent=data.video?`${data.video.width} × ${data.video.height} · ${data.video.duration_seconds.toFixed(1)} с · ${(data.video.size_bytes/1048576).toFixed(1)} МБ`:'';
      if(data.state==='failed')error(data.stage);
      if(Object.keys(data.downloads||{}).length)timer=setTimeout(()=>watch(id,version),5000);
    }else timer=setTimeout(()=>watch(id,version),1200);
  }catch(e){
    if(version!==generation||e.name==='AbortError')return;
    error('Соединение прервано. Проверим состояние снова через 5 секунд.');timer=setTimeout(()=>watch(id,version),5000);
  }
}
function startWatch(id){stopWatch();currentJob=id;watch(id,generation);}
$('cancel').addEventListener('click',async()=>{
  if(!currentJob)return;
  $('cancel').disabled=true;
  try{await fetch(`/api/jobs/${currentJob}/cancel`,{method:'POST'});startWatch(currentJob);}
  catch(e){error('Не удалось отменить задание. Повторите попытку.');}
  finally{$('cancel').disabled=false;}
});
$('form').addEventListener('submit',async event=>{
  event.preventDefault();updateSubmit();if($('submit').disabled)return;stopWatch();$('errors').hidden=true;setDownloads();$('metadata').textContent='';$('stage').textContent='Отправка задания…';$('progress').hidden=false;$('cancel').hidden=true;
  const body={text:$('text').value};for(const field of fields)body[field]=Number($(field).value);
  jobBusy=true;updateSubmit();
  try{
    const response=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await response.json();
    if(!response.ok){validatedText=null;jobBusy=false;updateSubmit();$('progress').hidden=true;$('stage').textContent='Файлы ещё не готовы';details(data);return;}
    normalization(data);startWatch(data.id);
  }catch(e){jobBusy=false;updateSubmit();$('progress').hidden=true;$('stage').textContent='Файлы ещё не готовы';error('Не удалось отправить запрос. Проверьте соединение.');}
});
localStorage.removeItem('polski-job');setDownloads();count();updateSubmit();
