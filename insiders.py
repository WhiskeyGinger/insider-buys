"""Insider buying alerts for Discord, from the SEC's EDGAR filings (free, official).

Company insiders (officers, directors, 10% owners) must report trades in their own company's stock on SEC Form 4,
usually within two business days. This scanner posts only OPEN-MARKET PURCHASES (transaction code "P"): the insider
spent their own money at the market price. Stock awards, option exercises, gifts and sales are ignored.

Each run reads EDGAR's "latest filings" feed for Form 4, fetches only filings it has not seen, and posts purchases
worth at least MIN_VALUE dollars. It also notes when several insiders bought the same stock within 30 days ("cluster
buying"), which is a stronger signal than one purchase. SEC rules ask automated clients to identify themselves with a
contact e-mail (SEC_CONTACT) and to stay under 10 requests a second.
Information only, not financial advice.
"""
import datetime as dt, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

FEED='https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb=&owner=include&count=100&output=atom&start=%d'
ARCHIVE='https://www.sec.gov/Archives/edgar/data/%s/%s/'
MIN_VALUE=float(os.environ.get('MIN_VALUE','100000') or 100000)
FEED_PAGES=int(os.environ.get('FEED_PAGES','4') or 4)
STATE_PATH=Path(os.environ.get('STATE_PATH','.state/insiders.json'))
CLUSTER_DAYS=30
REQUEST_GAP=0.15                    # ~7 requests/second, under the SEC's 10/s limit
TOP_TITLES=('chief executive','ceo','chief financial','cfo','president','chair','chief operating','coo','founder')

def ua():
    contact=os.environ.get('SEC_CONTACT','').strip()
    return {'User-Agent':'InsiderBuyAlerts/1.0 %s'%(contact or 'contact-not-set'),'Accept-Encoding':'identity'}

class Fetcher:
    """Polite HTTP: one request at a time, a pause between requests, retries on 429/5xx."""
    def __init__(self, opener=urllib.request.urlopen, sleep=time.sleep, gap=REQUEST_GAP):
        self.opener,self.sleep,self.gap,self.calls=opener,sleep,gap,0
    def __call__(self, url, attempts=3):
        for a in range(attempts):
            self.sleep(self.gap); self.calls+=1
            try:
                with self.opener(urllib.request.Request(url,headers=ua()),timeout=30) as r: return r.read().decode('utf-8','replace')
            except urllib.error.HTTPError as e:
                if e.code not in (429,500,502,503,504) or a==attempts-1: raise
            except (urllib.error.URLError,TimeoutError):
                if a==attempts-1: raise
            self.sleep(2**(a+1))

# --- feed ----------------------------------------------------------------------------------------------------------
ATOM='{http://www.w3.org/2005/Atom}'

def feed_entries(xml_text):
    """[(accession '0001234567-26-000123', filer_cik, index_url)] from EDGAR's Atom feed. Form 4 filings appear once
    per reporting person and once for the issuer; the accession number identifies the filing."""
    out=[]; root=ET.fromstring(xml_text)
    for e in root.findall(ATOM+'entry'):
        if not (e.findtext(ATOM+'title') or '').startswith('4 - '): continue   # the feed's type=4 also matches 424B etc.
        le=e.find(ATOM+'link'); link=le.get('href','') if le is not None else ''
        m=re.search(r'/data/(\d+)/(\d{18})/',link)
        acc=None
        idtag=(e.findtext(ATOM+'id') or '')
        m2=re.search(r'accession-number=(\d{10}-\d{2}-\d{6})',idtag)
        if m2: acc=m2.group(1)
        if m and not acc: d=m.group(2); acc='%s-%s-%s'%(d[:10],d[10:12],d[12:])
        if acc and m: out.append((acc,m.group(1),link))
    seen=set(); uniq=[]
    for acc,cik,link in out:
        if acc in seen: continue
        seen.add(acc); uniq.append((acc,cik,link))
    return uniq

def primary_xml(index_json):
    """Name of the Form 4 XML document in a filing's index.json."""
    items=(json.loads(index_json).get('directory') or {}).get('item') or []
    xmls=[i['name'] for i in items if str(i.get('name','')).lower().endswith('.xml') and not str(i.get('name','')).lower().startswith(('xsl','exfiling','filingsummary'))]
    return xmls[0] if xmls else None

# --- Form 4 ---------------------------------------------------------------------------------------------------------
def _t(node, path):
    x=node.find(path) if node is not None else None
    return (x.text or '').strip() if x is not None and x.text else ''
def _f(s):
    try: return float(str(s).replace(',',''))
    except (TypeError,ValueError): return None

def parse_form4(xml_text):
    """Open-market purchases in one Form 4: list of dicts (one per reporting owner, purchases summed)."""
    root=ET.fromstring(xml_text)
    if _t(root,'documentType') not in ('4','4/A'): return []
    issuer={'cik':_t(root,'issuer/issuerCik'),'name':_t(root,'issuer/issuerName'),'ticker':_t(root,'issuer/issuerTradingSymbol').upper()}
    owners=[]
    for o in root.findall('reportingOwner'):
        rel=o.find('reportingOwnerRelationship')
        flag=lambda k: _t(rel,k).lower() in ('1','true')
        owners.append({'name':_t(o,'reportingOwnerId/rptOwnerName'),'director':flag('isDirector'),'officer':flag('isOfficer'),
                       'ten_pct':flag('isTenPercentOwner'),'title':_t(rel,'officerTitle')})
    buys=[]
    for t in root.findall('nonDerivativeTable/nonDerivativeTransaction'):
        if _t(t,'transactionCoding/transactionCode')!='P' or _t(t,'transactionAmounts/transactionAcquiredDisposedCode/value')!='A': continue
        sh=_f(_t(t,'transactionAmounts/transactionShares/value')); px=_f(_t(t,'transactionAmounts/transactionPricePerShare/value'))
        if not sh or not px or sh<=0 or px<=0: continue
        buys.append({'date':_t(t,'transactionDate/value')[:10],'shares':sh,'price':px,'after':_f(_t(t,'postTransactionAmounts/sharesOwnedFollowingTransaction/value')),
                     'plan':_t(root,'aff10b5One').lower() in ('1','true')})
    if not buys or not owners: return []
    shares=sum(b['shares'] for b in buys); value=sum(b['shares']*b['price'] for b in buys)
    after=next((b['after'] for b in reversed(buys) if b['after'] is not None),None)
    dates=sorted(b['date'] for b in buys if b['date'])
    # Joint filings list several owners for the same shares; report the purchase once, under the first-named owner,
    # and mention the others.
    o=owners[0]
    return [{'issuer':issuer,'owner':o,'co_owners':[x['name'] for x in owners[1:]],'shares':shares,'value':value,'avg_price':value/shares,
             'after':after,'first_date':dates[0] if dates else None,'last_date':dates[-1] if dates else None,'trades':len(buys),
             'plan':any(b['plan'] for b in buys)}]

def role(o):
    t=o['title'].strip()
    if o['officer'] and t: return t
    if o['officer']: return 'Officer'
    if o['director']: return 'Director'
    if o['ten_pct']: return '10% owner'
    return 'Insider'

def is_top(o):
    t=o['title'].lower()
    return o['director'] or any(k in t for k in TOP_TITLES) or o['ten_pct']

# --- state ----------------------------------------------------------------------------------------------------------
def load_state(path=STATE_PATH):
    try: d=json.loads(Path(path).read_text())
    except (OSError,ValueError): d={}
    d.setdefault('seen',{}); d.setdefault('buys',[]); return d

def save_state(state, now, path=STATE_PATH):
    cutoff=(now-dt.timedelta(days=45)).isoformat()
    state['seen']={k:v for k,v in state['seen'].items() if v>=cutoff}
    state['buys']=[b for b in state['buys'] if b['at']>=cutoff]
    Path(path).parent.mkdir(parents=True,exist_ok=True); Path(path).write_text(json.dumps(state,indent=1,sort_keys=True))

def cluster_count(state, ticker, cik, now):
    """Distinct insiders who bought this stock in the last CLUSTER_DAYS, from what this scanner has posted."""
    since=(now-dt.timedelta(days=CLUSTER_DAYS)).isoformat()
    return len({b['owner'] for b in state['buys'] if b['at']>=since and (b['cik']==cik or (ticker and b['ticker']==ticker))})

# --- messages -------------------------------------------------------------------------------------------------------
def money(x):
    if x>=1e9: return '$%.2fB'%(x/1e9)
    if x>=1e6: return '$%.1fM'%(x/1e6)
    if x>=1e3: return '$%.0fK'%(x/1e3)
    return '$%.0f'%x

def nice_date(iso):
    try: d=dt.date.fromisoformat(iso); return d.strftime('%b ')+str(d.day)
    except (TypeError,ValueError): return iso or '?'

def embed(b, filed, url, cluster):
    i,o=b['issuer'],b['owner']; tk=' (%s)'%i['ticker'] if i['ticker'] else ''
    lines=['👤 **%s** — %s'%(o['name'].title() if o['name'].isupper() else o['name'],role(o))]
    if b['co_owners']: lines.append('👥 Joint filing with %s'%', '.join(n.title() if n.isupper() else n for n in b['co_owners'][:3]))
    lines.append('💰 Bought **%s** (%s shares at $%.2f%s)'%(money(b['value']),'{:,.0f}'.format(b['shares']),b['avg_price'],
                 ' avg' if b['trades']>1 else ''))
    when=nice_date(b['first_date']) if b['first_date']==b['last_date'] else '%s–%s'%(nice_date(b['first_date']),nice_date(b['last_date']))
    lines.append('📅 Traded %s · filed %s'%(when,nice_date(filed)))
    if b['after'] and b['after']>b['shares']:
        lines.append('📈 Now owns %s shares (+%d%%)'%('{:,.0f}'.format(b['after']),round(100*b['shares']/(b['after']-b['shares']))))
    elif b['after']: lines.append('📈 Now owns %s shares (new position)'%'{:,.0f}'.format(b['after']))
    if cluster>=2: lines.append('🔥 **%d insiders** bought %s in the last %d days'%(cluster,i['ticker'] or 'this stock',CLUSTER_DAYS))
    if b['plan']: lines.append('🗓️ Pre-scheduled (10b5-1 plan), a weaker signal')
    lines.append('👉 [SEC filing](%s)'%url)
    return {'title':'🕵️ INSIDER BUY · %s%s'%(i['name'],tk),'description':'\n'.join(lines),'color':3066993 if cluster<2 else 15105570}

HOW_TO_READ=('**How to read this**\n'
             '• An insider (executive, director or big owner) **bought their own company\'s stock with their own money** on the open market.\n'
             '• Insiders sell for many reasons, but usually buy for one: they think the price will go up. Several insiders buying (🔥) is a stronger sign.\n'
             '• It is not a guarantee. Insiders can be wrong, and filings arrive up to 2 business days after the trade. Not financial advice.')

def message(embeds):
    es=embeds[:9]; es.append({'description':HOW_TO_READ,'color':9807270})
    return {'username':'Insider Buy Alerts','allowed_mentions':{'parse':[]},'embeds':es}

def deliver(hook, payload, opener=urllib.request.urlopen, sleep=time.sleep, attempts=3):
    url=hook+('&' if '?' in hook else '?')+'wait=true'
    for a in range(attempts):
        try:
            req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','User-Agent':'InsiderBuyAlerts/1.0'},method='POST')
            with opener(req,timeout=30) as r: return 200<=r.status<300
        except urllib.error.HTTPError as e:
            if e.code==429: sleep(2); continue
            if e.code<500: return False
        except (urllib.error.URLError,TimeoutError): pass
        if a<attempts-1: sleep(2**(a+1))
    return False

# --- run ------------------------------------------------------------------------------------------------------------
def run(hook, now=None, fetch=None, send=deliver, state_path=STATE_PATH, max_filings=150):
    now=now or dt.datetime.now(dt.timezone.utc); fetch=fetch or Fetcher(); state=load_state(state_path)
    rep={'feed_entries':0,'new_filings':0,'parsed':0,'purchases':0,'posted':0,'below_min':0,'not_top':0,'errors':[]}
    entries=[]
    for p in range(FEED_PAGES):
        try: body=fetch(FEED%(p*100)); page=feed_entries(body)
        except Exception as e: rep['errors'].append('feed page %d: %s'%(p,type(e).__name__)); break
        entries+=page
        if '<entry' not in body: break   # past the end of the feed (pages also hold non-Form-4 filings, so count raw entries)
    rep['feed_entries']=len(entries)
    fresh=[(a,c,l) for a,c,l in entries if a not in state['seen']][:max_filings]; rep['new_filings']=len(fresh)
    out=[]
    for acc,cik,link in fresh:
        folder=ARCHIVE%(cik,acc.replace('-',''))
        try:
            name=primary_xml(fetch(folder+'index.json'))
            if not name: state['seen'][acc]=now.isoformat(); continue
            buys=parse_form4(fetch(folder+name)); rep['parsed']+=1
        except Exception as e: rep['errors'].append('%s: %s'%(acc,type(e).__name__)); continue
        state['seen'][acc]=now.isoformat()
        for b in buys:
            rep['purchases']+=1
            if b['value']<MIN_VALUE: rep['below_min']+=1; continue
            if not is_top(b['owner']): rep['not_top']+=1; continue
            url=folder+acc+'-index.htm'
            state['buys'].append({'at':now.isoformat(),'owner':b['owner']['name'],'cik':b['issuer']['cik'],'ticker':b['issuer']['ticker'],'value':b['value'],
                                  'company':b['issuer']['name'],'role':role(b['owner']),'date':b.get('last_date'),'url':url})
            out.append(embed(b,now.date().isoformat(),url,cluster_count(state,b['issuer']['ticker'],b['issuer']['cik'],now)))
    for i in range(0,len(out),9):
        if send(hook,message(out[i:i+9])): rep['posted']+=len(out[i:i+9])
    rep['requests']=getattr(fetch,'calls',None)
    save_state(state,now,state_path); return rep

def recap_data(state, now, days=8):
    """Posted buys from the last `days` days, for the weekly recap (published to this repo's `data` branch)."""
    since=(now-dt.timedelta(days=days)).isoformat()
    rows=[{k:b.get(k) for k in ('at','owner','role','company','ticker','value','date','url')} for b in state['buys'] if b['at']>=since and b.get('company')]
    for r in rows: r['insiders']=len({x['owner'] for x in state['buys'] if x.get('ticker')==r['ticker'] and r['ticker'] and x['at']>=since})
    return {'kind':'insider-buys','buys':sorted(rows,key=lambda r:-(r['value'] or 0))}

def sample(now):
    b={'issuer':{'cik':'1','name':'Example Industries','ticker':'EXMP'},'owner':{'name':'Jane Example','director':False,'officer':True,'ten_pct':False,'title':'Chief Executive Officer'},
       'co_owners':[],'shares':40000,'value':1.2e6,'avg_price':30.0,'after':250000,'first_date':'2026-11-24','last_date':'2026-11-24','trades':1,'plan':False}
    m=message([embed(b,'2026-11-26','https://www.sec.gov/',3)])
    m['embeds'][0]['title']='🧪 SAMPLE — '+m['embeds'][0]['title']
    m['content']='🧪 **Sample with a made-up company** — this is what real alerts look like.'
    return m

def main():
    mode=os.environ.get('MODE','run')
    if mode=='probe':
        f=Fetcher(); body=f(FEED%0); es=feed_entries(body); print('feed entries',len(es),es[:2])
        for acc,cik,link in es[:6]:
            folder=ARCHIVE%(cik,acc.replace('-','')); name=primary_xml(f(folder+'index.json'))
            doc=f(folder+name) if name else ''
            print(acc,name,'ownershipDocument' in doc,parse_form4(doc) if doc else None)
        return
    hook=os.environ.get('DISCORD_WEBHOOK_URL')
    if not hook: sys.exit('DISCORD_WEBHOOK_URL is not set (Settings → Secrets and variables → Actions)')
    if not os.environ.get('SEC_CONTACT'): print('warning: SEC_CONTACT not set; the SEC may refuse requests without a contact e-mail')
    if mode=='sample': ok=deliver(hook,sample(dt.datetime.now(dt.timezone.utc))); print('sample delivered' if ok else 'sample failed'); sys.exit(0 if ok else 1)
    rep=run(hook); print(json.dumps(rep))
    Path('recap.json').write_text(json.dumps(recap_data(load_state(),dt.datetime.now(dt.timezone.utc)),indent=1,sort_keys=True))
    if rep['errors'] and not rep['feed_entries']: sys.exit('Could not read the SEC feed: '+'; '.join(rep['errors'][:3]))
if __name__=='__main__': main()
