const { chromium } = require('./frontend-check/node_modules/playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const dist = path.join(__dirname, 'frontend-check/dist');
  const source = 'sintetico/reglas.txt#0';
  const content = 'INICIO COMPLETO\n\n**Documento de prueba [[sintetico/reglas.txt#0]]**\n\n' +
    '3. Tercer paso\n7. Séptimo paso\n\n' +
    Array.from({length: 100}, (_, i) => `Párrafo ${i}: texto sintético completo con condiciones y excepciones.`).join('\n\n') +
    '\n\n```texto\n' + 'PALABRA'.repeat(300) + '\n```\n\n' +
    '| ' + Array.from({length:12},(_,i)=>'Columna '+i).join(' | ') + ' |\n' +
    '| ' + Array(12).fill('---').join(' | ') + ' |\n' +
    '| ' + Array(12).fill('Valor sintético completo').join(' | ') + ' |\n\nFIN COMPLETO';
  const me = {user_id:'synthetic',username:'synthetic',display_name:'Prueba local',auth_source:'local_test',roles:[],permissions:[],allowed_categories:[],category_wildcard:false,csrf_token:'synthetic'};
  const conversation = {id:'synthetic',title:'Prueba visual local',created_at:'2026-10-06',updated_at:'2026-10-06',attachments:[],messages:[
    {id:'response',role:'assistant',content,intent:'documental',answer_basis:'documented',sources:[{source_id:source,filename:'reglas.txt',category:'sintetico',page_or_sheet:'pagina 1',score:1,label:'reglas.txt',scope:'corporate'}]},
  ]};
  const browser = await chromium.launch({channel:'msedge',headless:true});
  const results = [];
  try {
    for (const width of [1280,390]) {
      const page = await browser.newPage({viewport:{width,height:900},reducedMotion:"reduce"});
      const errors=[];
      page.on('pageerror', error=>errors.push(String(error)));
      await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.origin !== 'http://matrix.synthetic') return route.abort();
        if (url.pathname === '/api/v1/me') return route.fulfill({json:me});
        if (url.pathname === '/api/v1/conversations') return route.fulfill({json:[conversation]});
        if (url.pathname === '/api/v1/conversations/synthetic') return route.fulfill({json:conversation});
        if (url.pathname.startsWith('/api/')) return route.fulfill({status:404,json:{code:'not_found'}});
        const relative = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
        const file = path.resolve(dist,relative);
        assert.ok(file.startsWith(dist + path.sep));
        const types = {'.html':'text/html','.js':'application/javascript','.css':'text/css','.png':'image/png','.jpg':'image/jpeg'};
        return route.fulfill({body:await fs.readFile(file),contentType:types[path.extname(file)] || 'application/octet-stream'});
      });
      await page.goto('http://matrix.synthetic');
      await page.getByTestId('identity-user').waitFor();
      // Force the selected synthetic conversation only through the product UI.
      const item = page.getByRole('button', {name:/Prueba visual local/}).first();
      await item.waitFor();
      const itemBounds = await item.boundingBox();
      if (!itemBounds || itemBounds.x < 0) {
        await page.getByRole('button', {name:"Conversaciones",exact:true}).click();
      }
      await item.click();
      const response=page.getByTestId('message-assistant');
      await response.waitFor();
      assert.ok((await response.textContent()).includes('INICIO COMPLETO'));
      assert.ok((await response.textContent()).includes('FIN COMPLETO'));
      assert.equal(await response.locator('li[value="7"]').count(),1);
      assert.equal(await response.locator('strong .citation').count(),1);
      const end=page.getByText('FIN COMPLETO',{exact:true});
      await end.scrollIntoViewIfNeeded();
      await end.waitFor({state:'visible'});
      const bounds=await end.boundingBox();
      assert.ok(bounds && bounds.y >= 0 && bounds.y+bounds.height <= 901);
      const layout=await page.evaluate(()=>({
        viewport:innerWidth,width:document.documentElement.scrollWidth,
        responseHeight:document.querySelector('[data-testid="message-assistant"]').getBoundingClientRect().height,
        tableCount:document.querySelectorAll('[role="region"][tabindex="0"]').length,
      }));
      assert.ok(layout.width <= width+1, JSON.stringify(layout));
      assert.ok(layout.responseHeight>4000);
      const table=page.getByRole('region',{name:'Tabla de la respuesta'});
      await table.focus();
      await table.press('ArrowRight');
      const dimensions=await table.evaluate(el=>({client:el.clientWidth,scroll:el.scrollWidth,cell:el.querySelector('th').getBoundingClientRect().width}));
      assert.ok(dimensions.scroll>dimensions.client);
      assert.ok(dimensions.cell>=120);
      await table.evaluate(el=>{el.scrollLeft=el.scrollWidth;});
      await end.scrollIntoViewIfNeeded();
      assert.equal(errors.length,0,errors.join('\n'));
      await page.screenshot({path:path.join(__dirname,`visual-${width}-end.png`)});
      await page.getByText('INICIO COMPLETO',{exact:true}).scrollIntoViewIfNeeded();
      await page.screenshot({path:path.join(__dirname,`visual-${width}-start.png`)});
      results.push({width,passed:true,...layout});
      await page.close();
    }
    await fs.writeFile(path.join(__dirname,'visual-results.json'),JSON.stringify({synthetic:true,network:'all routes fulfilled from isolated build and synthetic JSON',results},null,2));
    console.log(JSON.stringify(results));
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
