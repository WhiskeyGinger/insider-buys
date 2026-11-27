import unittest, datetime as dt, json, tempfile
from pathlib import Path
import insiders as I

FEED='''<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Latest Filings</title>
<entry><title>4 - Smith Jane (0001900001) (Reporting)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1900001/000190000126000010/0001900001-26-000010-index.htm"/>
<id>urn:tag:sec.gov,2008:accession-number=0001900001-26-000010</id></entry>
<entry><title>4 - ACME CORP (0000123456) (Issuer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/123456/000190000126000010/0001900001-26-000010-index.htm"/>
<id>urn:tag:sec.gov,2008:accession-number=0001900001-26-000010</id></entry>
<entry><title>4 - Doe John (0001900002) (Reporting)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1900002/000190000226000003/0001900002-26-000003-index.htm"/>
<id>urn:tag:sec.gov,2008:accession-number=0001900002-26-000003</id></entry>
</feed>'''
INDEX=json.dumps({'directory':{'item':[{'name':'0001900001-26-000010-index.htm'},{'name':'xslF345X05'},{'name':'wk-form4_1.xml'}]}})
def form4(owner='SMITH JANE',title='Chief Executive Officer',officer='1',director='0',code='P',ad='A',shares='40000',price='30.00',after='250000',ticker='ACME',plan='0'):
    return f'''<?xml version="1.0"?><ownershipDocument><documentType>4</documentType><aff10b5One>{plan}</aff10b5One>
<issuer><issuerCik>0000123456</issuerCik><issuerName>ACME CORP</issuerName><issuerTradingSymbol>{ticker}</issuerTradingSymbol></issuer>
<reportingOwner><reportingOwnerId><rptOwnerCik>0001900001</rptOwnerCik><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>{director}</isDirector><isOfficer>{officer}</isOfficer><isTenPercentOwner>0</isTenPercentOwner><officerTitle>{title}</officerTitle></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable><nonDerivativeTransaction><securityTitle><value>Common Stock</value></securityTitle><transactionDate><value>2026-11-24</value></transactionDate>
<transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>{shares}</value></transactionShares><transactionPricePerShare><value>{price}</value></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode></transactionAmounts>
<postTransactionAmounts><sharesOwnedFollowingTransaction><value>{after}</value></sharesOwnedFollowingTransaction></postTransactionAmounts></nonDerivativeTransaction></nonDerivativeTable>
</ownershipDocument>'''
NOW=dt.datetime(2026,11,27,15,0,tzinfo=dt.timezone.utc)

class Parse(unittest.TestCase):
    def test_feed_dedupes_by_accession(self):
        es=I.feed_entries(FEED); self.assertEqual([a for a,_,_ in es],['0001900001-26-000010','0001900002-26-000003']); self.assertEqual(es[0][1],'1900001')
    def test_primary_xml(self): self.assertEqual(I.primary_xml(INDEX),'wk-form4_1.xml')
    def test_purchase(self):
        b=I.parse_form4(form4())[0]; self.assertEqual((b['shares'],b['value'],b['after']),(40000,1.2e6,250000)); self.assertEqual(I.role(b['owner']),'Chief Executive Officer')
    def test_sales_awards_and_exercises_ignored(self):
        for code,ad in (('S','D'),('A','A'),('M','A'),('G','D')): self.assertEqual(I.parse_form4(form4(code=code,ad=ad)),[],code)
    def test_who_counts(self):
        self.assertTrue(I.is_top({'title':'CFO','director':False,'ten_pct':False})); self.assertTrue(I.is_top({'title':'','director':True,'ten_pct':False}))
        self.assertFalse(I.is_top({'title':'VP, Marketing','director':False,'ten_pct':False}))

class Run(unittest.TestCase):
    def setUp(self): self.d=tempfile.TemporaryDirectory(); self.p=Path(self.d.name,'s.json'); self.sent=[]; self.docs={}
    def tearDown(self): self.d.cleanup()
    def fetch(self,url):
        if 'browse-edgar' in url: return FEED if url.endswith('start=0') else '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        if url.endswith('index.json'): return INDEX
        return self.docs.get(url.split('/')[-2],form4())
    def send(self,h,m): self.sent.append(m); return True
    def test_posts_once_with_explainer(self):
        self.docs['000190000226000003']=form4(owner='DOE JOHN',title='',officer='0',director='1',shares='10000',price='25')
        r=I.run('H',NOW,self.fetch,self.send,self.p); self.assertEqual(r['posted'],2)
        e=self.sent[0]['embeds']; self.assertEqual(e[0]['title'],'🕵️ INSIDER BUY · ACME CORP (ACME)')
        d=e[0]['description']; self.assertIn('👤 **Smith Jane** — Chief Executive Officer',d); self.assertIn('💰 Bought **$1.2M** (40,000 shares at $30.00)',d)
        self.assertIn('📈 Now owns 250,000 shares (+19%)',d); self.assertIn('How to read this',e[-1]['description'])
        self.assertIn('🔥 **2 insiders** bought ACME',e[1]['description'])
        self.assertEqual(I.run('H',NOW,self.fetch,self.send,self.p)['posted'],0)
    def test_small_and_junior_buys_skipped(self):
        self.docs['000190000126000010']=form4(shares='100',price='10')
        self.docs['000190000226000003']=form4(owner='X',title='VP Sales',shares='10000',price='50')
        r=I.run('H',NOW,self.fetch,self.send,self.p); self.assertEqual((r['posted'],r['below_min'],r['not_top']),(0,1,1)); self.assertEqual(self.sent,[])
    def test_feed_failure_reported(self):
        def bad(url): raise OSError()
        r=I.run('H',NOW,bad,self.send,self.p); self.assertEqual(r['feed_entries'],0); self.assertTrue(r['errors'])
    def test_sample_labelled(self): self.assertTrue(I.sample(NOW)['embeds'][0]['title'].startswith('🧪 SAMPLE'))

if __name__=='__main__': unittest.main()
