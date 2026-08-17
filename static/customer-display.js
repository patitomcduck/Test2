(() => {
  const idle=document.getElementById('customer-idle');
  const sale=document.getElementById('customer-sale');
  const trade=document.getElementById('customer-trade');
  const thanks=document.getElementById('customer-thanks');
  const money=n=>`$${Number(n||0).toLocaleString('es-MX',{minimumFractionDigits:2,maximumFractionDigits:2})}`;
  const show=el=>{[idle,sale,trade,thanks].forEach(x=>x.hidden=x!==el)};
  let lastRaw='';
  function paymentName(p){return ({cash:'Efectivo',card:'Tarjeta',other:'Otro',credit:'Crédito de tienda'})[p]||'—';}
  function render(data){
    const raw=JSON.stringify(data); if(raw===lastRaw) return; lastRaw=raw;
    if(data.mode==='sale' && Array.isArray(data.items) && data.items.length){
      show(sale);
      const box=document.getElementById('customer-items'); box.innerHTML=''; let count=0;
      data.items.forEach((item,i)=>{count+=Number(item.qty||0); const row=document.createElement('div'); row.className='customer-item'; const idx=document.createElement('div'); idx.className='customer-item-index'; idx.textContent=String(i+1); const img=document.createElement(item.image?'img':'div'); img.className='customer-item-image'; if(item.image){img.src=item.image;img.alt='';} else {img.textContent='▣';} const copy=document.createElement('div'); copy.className='customer-item-copy'; const strong=document.createElement('strong'); strong.textContent=item.name||'Producto'; const small=document.createElement('small'); small.textContent=item.sku||''; copy.append(strong,small); const qty=document.createElement('div'); qty.className='customer-item-qty'; qty.textContent=String(item.qty||1); const price=document.createElement('div'); price.className='customer-item-price'; price.textContent=money(Number(item.price||0)*Number(item.qty||1)); row.append(idx,img,copy,qty,price); box.appendChild(row);});
      document.getElementById('customer-item-count').textContent=`${count} ${count===1?'artículo':'artículos'}`;
      document.getElementById('customer-subtotal').textContent=money(data.subtotal); document.getElementById('customer-discount').textContent=`−${money(data.discount)}`; document.getElementById('customer-total').textContent=money(data.total); document.getElementById('customer-payment-method').textContent=paymentName(data.payment); document.getElementById('customer-received').textContent=data.payment==='cash'?money(data.received):'—'; document.getElementById('customer-change').textContent=data.payment==='cash'?money(data.change):'—';
      return;
    }
    if(data.mode==='complete'){
      show(thanks); document.getElementById('thanks-total').textContent=money(data.total); document.getElementById('thanks-sale-number').textContent=data.sale_number?`Folio ${data.sale_number}`:''; return;
    }
    if(data.mode==='trade' || data.mode==='trade_complete'){
      show(trade);
      const market=Number(data.market_total||0), credit=Number(data.credit_total ?? market*window.TRADE_CREDIT_RATE), cash=Number(data.cash_total ?? market*window.TRADE_CASH_RATE);
      document.getElementById('trade-display-market').textContent=money(market);
      document.getElementById('trade-display-credit').textContent=money(credit);
      document.getElementById('trade-display-cash').textContent=money(cash);
      const items = Array.isArray(data.items) ? data.items : [];
      const itemCopy = document.getElementById('trade-display-items-copy');
      if(items.length){
        const totalQty = items.reduce((sum,i)=>sum+Number(i.qty||0),0);
        itemCopy.textContent = `${items[0].name}${items.length>1?` y ${items.length-1} más`:''} · ${totalQty} ${totalQty===1?'pieza':'piezas'}`;
      } else {
        itemCopy.textContent = 'Captura el artículo y su valor de mercado.';
      }
      const selected=document.getElementById('trade-display-selected');
      const creditBox=document.getElementById('trade-option-credit');
      const cashBox=document.getElementById('trade-option-cash');
      creditBox.classList.remove('selected'); cashBox.classList.remove('selected');
      const useCash = (data.payout==='cash' || data.payout_type==='cash');
      if(useCash) cashBox.classList.add('selected'); else creditBox.classList.add('selected');
      if(data.mode==='trade_complete'){
        selected.textContent=`✓ Intercambio registrado · ${useCash?`Efectivo ${Math.round(window.TRADE_CASH_RATE*100)}%`:`Crédito ${Math.round(window.TRADE_CREDIT_RATE*100)}%`} · ${money(data.offer_total)}`;
        selected.className='trade-selected success-text';
      } else {
        selected.textContent=useCash?`Opción seleccionada: Efectivo · ${money(cash)}`:`Opción seleccionada: Crédito en producto · ${money(credit)}`;
        selected.className='trade-selected';
      }
      return;
    }
    show(idle);
  }
  async function poll(){ try { const r=await fetch('/api/customer-display',{cache:'no-store'}); if(r.ok) render(await r.json()); } catch(e) {} finally { setTimeout(poll,500); } }
  poll();
})();
