(() => {
  const cards=[...document.querySelectorAll('.product-card')];
  const search=document.getElementById('product-search');
  const clearSearch=document.getElementById('clear-search');
  const cartBox=document.getElementById('cart-items');
  const subtotalEl=document.getElementById('subtotal');
  const discountEl=document.getElementById('discount');
  const discountDisplay=document.getElementById('discount-display');
  const totalEl=document.getElementById('total');
  const cashReceived=document.getElementById('cash-received');
  const changePreview=document.getElementById('change-preview');
  const cashBox=document.getElementById('cash-box');
  const checkoutBtn=document.getElementById('checkout');
  const message=document.getElementById('checkout-message');
  const emailInput=document.getElementById('customer-email');
  const phoneInput=document.getElementById('customer-phone');
  const customerSelect=document.getElementById('sale-customer');
  const creditRow=document.getElementById('credit-sale-row');
  const creditUse=document.getElementById('credit-use');
  const creditAvailable=document.getElementById('credit-available');
  const creditRemaining=document.getElementById('credit-remaining');
  let category='all', payment='cash', lastSale=null, displayLocked=false, syncTimer=null;
  const cart=new Map();
  const money=n=>`$${Number(n||0).toLocaleString('es-MX',{minimumFractionDigits:2,maximumFractionDigits:2})}`;
  const esc=s=>String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));

  function filterCards(){
    const q=search.value.trim().toLowerCase();
    cards.forEach(card=>{
      const categoryOk=category==='all'||card.dataset.category===category;
      const haystack=`${card.dataset.name} ${card.dataset.sku} ${card.dataset.meta}`.toLowerCase();
      card.hidden=!(categoryOk&&haystack.includes(q));
    });
  }
  function totals(){
    let subtotal=0;
    for(const i of cart.values()) subtotal+=i.price*i.qty;
    const discount=Math.max(0,Number(discountEl.value||0));
    const total=Math.max(0,subtotal-discount);
    const received=Math.max(0,Number(cashReceived.value||0));
    const selected=customerSelect?.selectedOptions?.[0];
    const available=Math.max(0,Number(selected?.dataset?.credit||0));
    const credit=Math.min(Math.max(0,Number(creditUse?.value||0)),available,total);
    const remaining=Math.max(0,total-credit);
    const change=Math.max(0,received-remaining);
    subtotalEl.textContent=money(subtotal); discountDisplay.textContent=`−${money(discount)}`; totalEl.textContent=money(total); changePreview.textContent=money(change);
    if(creditAvailable)creditAvailable.textContent=money(available); if(creditRemaining)creditRemaining.textContent=money(remaining);
    return {subtotal,discount,total,received,change,credit,remaining,available};
  }
  function customerPayload(){
    const t=totals();
    if(!cart.size) return {mode:'idle'};
    return {mode:'sale',items:[...cart.values()].map(i=>({name:i.name,sku:i.sku,qty:i.qty,price:i.price,image:i.image||''})),subtotal:t.subtotal,discount:t.discount,total:t.total,payment,received:payment==='cash'?t.received:null,change:payment==='cash'?t.change:null};
  }
  function pushDisplay(payload, immediate=false){
    if(displayLocked&&!immediate)return;
    clearTimeout(syncTimer);
    const send=()=>fetch('/api/customer-display',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).catch(()=>{});
    if(immediate)send(); else syncTimer=setTimeout(send,90);
  }
  function syncDisplay(){pushDisplay(customerPayload());}
  function renderCart(){
    if(!cart.size){cartBox.innerHTML='<div class="cart-empty"><span>🛒</span><strong>Carrito vacío</strong><small>Agrega un producto para comenzar</small></div>';totals();syncDisplay();return;}
    cartBox.innerHTML=[...cart.values()].map(i=>`<div class="cart-line" data-cart-id="${i.id}"><div class="cart-thumb">${i.image?`<img src="${esc(i.image)}" alt="">`:'▣'}</div><div class="cart-copy"><strong>${esc(i.name)}</strong><span>${esc(i.sku)}</span></div><div class="cart-line-right"><div class="cart-price">${money(i.price*i.qty)}</div><div class="qty-control"><button data-act="minus">−</button><b>${i.qty}</b><button data-act="plus">+</button></div></div></div>`).join('');
    totals(); syncDisplay();
  }
  cards.forEach(card=>card.addEventListener('click',()=>{
    const id=Number(card.dataset.id), existing=cart.get(id), stock=Number(card.dataset.stock);
    if(existing){if(existing.qty<stock)existing.qty++;}
    else cart.set(id,{id,name:card.dataset.name,sku:card.dataset.sku,price:Number(card.dataset.price),stock,image:card.dataset.image||'',qty:1});
    renderCart();
  }));
  cartBox.addEventListener('click',e=>{
    const btn=e.target.closest('button[data-act]'); if(!btn)return;
    const id=Number(btn.closest('[data-cart-id]').dataset.cartId), item=cart.get(id); if(!item)return;
    if(btn.dataset.act==='plus'&&item.qty<item.stock)item.qty++;
    if(btn.dataset.act==='minus'){item.qty--;if(item.qty<=0)cart.delete(id);}
    renderCart();
  });
  search.addEventListener('input',filterCards);
  search.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();const exact=cards.find(c=>!c.hidden&&c.dataset.sku.toLowerCase()===search.value.trim().toLowerCase());const first=exact||cards.find(c=>!c.hidden);if(first){first.click();search.select();}}});
  clearSearch.addEventListener('click',()=>{search.value='';filterCards();search.focus();});
  document.getElementById('quick-filters').addEventListener('click',e=>{const chip=e.target.closest('[data-category]');if(!chip)return;document.querySelectorAll('.chip').forEach(c=>c.classList.remove('active'));chip.classList.add('active');category=chip.dataset.category;filterCards();});
  document.getElementById('empty-cart').addEventListener('click',()=>{cart.clear();renderCart();});
  discountEl.addEventListener('input',()=>{totals();syncDisplay();}); cashReceived.addEventListener('input',()=>{totals();syncDisplay();});
  customerSelect?.addEventListener('change',()=>{const opt=customerSelect.selectedOptions[0];const has=!!customerSelect.value;creditRow.hidden=!has;creditUse.value='0';if(has){if(!emailInput.value)emailInput.value=opt.dataset.email||'';if(!phoneInput.value)phoneInput.value=opt.dataset.phone||'';}totals();syncDisplay();});
  creditUse?.addEventListener('input',()=>{const t=totals();if(Number(creditUse.value||0)>t.available)creditUse.value=t.available.toFixed(2);totals();syncDisplay();});
  document.querySelectorAll('.payment-btn').forEach(btn=>btn.addEventListener('click',()=>{document.querySelectorAll('.payment-btn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');payment=btn.dataset.method;cashBox.hidden=payment!=='cash';syncDisplay();}));

  const dialog=document.getElementById('receipt-dialog');
  const preview=document.getElementById('receipt-preview');
  const shareStatus=document.getElementById('receipt-share-status');
  const phoneDigits=()=>String(phoneInput.value||'').replace(/\D/g,'');
  function openMailto(){if(!lastSale)return;const to=emailInput.value.trim();location.href=`mailto:${encodeURIComponent(to)}?subject=${encodeURIComponent(`Tu recibo ${window.STORE_NAME} · ${lastSale.sale_number}`)}&body=${encodeURIComponent(lastSale.receipt_text)}`;}
  async function sendEmail(){if(!lastSale)return;const email=emailInput.value.trim();if(!email){shareStatus.textContent='Escribe el correo del cliente.';return;}shareStatus.textContent='Enviando…';try{const r=await fetch(`/api/sales/${lastSale.sale_id}/email`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})});const d=await r.json();if(!r.ok){if(d.fallback){shareStatus.textContent='SMTP no configurado; abriré tu app de correo.';openMailto();return;}throw new Error(d.error||'No se pudo enviar');}shareStatus.textContent='✓ Recibo enviado por correo.';}catch(err){shareStatus.textContent=err.message;}}
  document.getElementById('receipt-email').addEventListener('click',sendEmail);
  document.getElementById('receipt-whatsapp').addEventListener('click',()=>{if(!lastSale)return;const p=phoneDigits(),url=p?`https://wa.me/${p}?text=${encodeURIComponent(lastSale.receipt_text)}`:`https://wa.me/?text=${encodeURIComponent(lastSale.receipt_text)}`;window.open(url,'_blank');});
  document.getElementById('receipt-new-sale').addEventListener('click',()=>window.location.reload());
  dialog.addEventListener('close',()=>{if(lastSale)window.location.reload();});

  checkoutBtn.addEventListener('click',async()=>{
    if(!window.POS_HAS_SHIFT){message.textContent='Abre caja antes de cobrar.';message.className='checkout-message error-text';return;}
    if(!cart.size){message.textContent='El carrito está vacío.';message.className='checkout-message error-text';return;}
    checkoutBtn.disabled=true;message.textContent='Procesando…';message.className='checkout-message';
    const t=totals();
    const displayItems=[...cart.values()].map(i=>({name:i.name,sku:i.sku,qty:i.qty,price:i.price,image:i.image||''}));
    const body={items:[...cart.values()].map(i=>({product_id:i.id,qty:i.qty})),payment_method:payment,discount_mxn:t.discount,amount_received_mxn:payment==='cash'&&t.remaining>0?t.received:null,customer_email:emailInput.value.trim(),customer_phone:phoneInput.value.trim(),customer_id:customerSelect?.value||null,store_credit_mxn:t.credit};
    try{
      const res=await fetch('/api/checkout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),data=await res.json();
      if(!res.ok)throw new Error(data.error||'No se pudo completar la venta');
      lastSale=data; displayLocked=true;
      pushDisplay({mode:'complete',sale_number:data.sale_number,items:displayItems,subtotal:data.subtotal_mxn,discount:data.discount_mxn,total:data.total_mxn,payment,received:payment==='cash'?t.received:null,change:data.change_mxn},true);
      document.getElementById('receipt-title').textContent=`${data.sale_number} · ${money(data.total_mxn)}`;preview.textContent=data.receipt_text;shareStatus.textContent='';dialog.showModal();cart.clear();discountEl.value='0';cashReceived.value='';renderCart();message.textContent=`✓ ${data.sale_number}`;message.className='checkout-message success-text';
    }catch(err){message.textContent=err.message;message.className='checkout-message error-text';}
    finally{checkoutBtn.disabled=false;}
  });
  renderCart();
})();
