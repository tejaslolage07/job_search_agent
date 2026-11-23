"""Candidate India-HQ software employers, banded by size. FROM MODEL KNOWLEDGE, approximate, unverified.
Band rule (revenue if known, else employees):  large >= Rs10,000cr or >=10k staff;  medium Rs1,000-10,000cr or 1k-10k;
small Rs100-1,000cr or 200-1k;  micro < Rs100cr or < 200.  basis: R = revenue-based, E = employee-count-based.
Format per line: Name | basis"""

RAW = {
"large": """
TCS|R
Infosys|R
Wipro|R
HCLTech|R
Tech Mahindra|R
LTIMindtree|R
Hexaware|R
Mphasis|R
Persistent Systems|R
Coforge|R
Flipkart|R
Swiggy|R
Eternal (Zomato)|R
Reliance Jio|R
Reliance Retail|R
Tata Digital|R
Tata 1mg|R
Tata Play|R
Tata Communications|R
Larsen & Toubro Technology Services|R
L&T Infotech|R
Mahindra & Mahindra|R
Mahindra Group|R
Bharti Airtel|R
Vodafone Idea|R
HDFC Bank|R
ICICI Bank|R
Axis Bank|R
State Bank of India|R
Kotak Mahindra Bank|R
IndusInd Bank|R
Yes Bank|R
Bajaj Finserv|R
Bajaj Finance|R
HDFC Life|R
ICICI Prudential|R
SBI Life|R
LIC|R
Aditya Birla Group|R
Adani Group|R
JSW|R
Ola|R
Ola Electric|R
Paytm|R
PhonePe|R
Zoho|R
Dream11|R
Amazon India Development Centre|E
Myntra|R
Meesho|R
Nykaa|R
PB Fintech (PolicyBazaar)|R
BigBasket|R
Zepto|R
Blinkit|R
Delhivery|R
Ola Cabs|R
Byju's|R
Jio Platforms|R
Sify Technologies|R
Tata Elxsi|R
Tata Technologies|R
Tata Motors|R
Tata Steel|R
Tata Power|R
Maruti Suzuki|R
Infosys BPM|R
Genpact India|E
Firstsource|R
WNS|R
Hinduja Global Solutions|R
Quess Corp|R
Cyient|R
Zensar Technologies|R
Birlasoft|R
Sonata Software|R
KPIT Technologies|R
Mastek|R
Happiest Minds|R
Ramco Systems|R
Intellect Design Arena|R
Nucleus Software|R
Oracle Financial Services Software|R
Tanla Platforms|R
Route Mobile|R
Affle|R
Info Edge (Naukri)|R
Zomato|R
CarTrade|R
MakeMyTrip|R
Ixigo|R
Nazara Technologies|R
Jio Haptik|R
Bharat Petroleum|R
Indian Oil|R
ONGC|R
ISRO|E
DRDO|E
Bharat Electronics|R
Hindustan Aeronautics|R
Havells|R
Asian Paints|R
Titan|R
ITC|R
Hindustan Unilever|R
Nestle India|R
Britannia|R
Godrej|R
Hero MotoCorp|R
TVS Motor|R
Bajaj Auto|R
Eicher Motors|R
Ashok Leyland|R
Apollo Hospitals|R
Max Healthcare|R
Dr. Reddy's|R
Sun Pharma|R
Cipla|R
Lupin|R
Biocon|R
Trent|R
DMart (Avenue Supermarts)|R
Pidilite|R
UltraTech Cement|R
Grasim|R
Hindalco|R
Vedanta|R
NTPC|R
Power Grid|R
Coal India|R
IRCTC|R
Cars24|R
Spinny|R
OYO|R
Lenskart|R
Urban Company|R
Zerodha|R
Upstox|R
Groww|R
Razorpay|R
CRED|R
BharatPe|R
Pine Labs|R
Juspay|R
InMobi|R
ShareChat|R
Moj|R
Dailyhunt|R
VerSe Innovation|R
Freshworks|R
Chargebee|R
Postman|R
BrowserStack|R
Zeta|R
Slice|R
Jupiter|R
Navi|R
KreditBee|R
PayU India|R
MobiKwik|R
Lendingkart|R
Shiprocket|R
Ecom Express|R
Xpressbees|R
Licious|R
Country Delight|R
FirstCry|R
Mamaearth (Honasa)|R
boAt|R
PharmEasy|R
Practo|R
Cult.fit|R
Unacademy|R
PhysicsWallah|R
upGrad|R
Vedantu|R
Eruditus|R
Simplilearn|R
Redbus|R
Rapido|R
Porter|R
Park+|R
Ather Energy|R
Bounce|R
Yulu|R
Vahan|R
""",
"medium": """
Mindtree|R
Mphasis NeXT|R
Cognizant India|E
Capgemini India|E
Accenture India|E
Tata Consultancy Services iON|R
Newgen Software|R
Kellton Tech|R
Sasken Technologies|R
Subex|R
Saksoft|R
Datamatics|R
eClerx|R
Majesco|R
3i Infotech|R
Allied Digital|R
Rategain|R
Netweb Technologies|R
Latent View Analytics|R
Mphasis Digital|R
Tata Elxsi Design|R
Innovaccer India|E
Fractal Analytics|R
Tredence|R
Mu Sigma|R
Course5 Intelligence|R
Sigmoid|E
LatentView|R
Tiger Analytics|E
ThoughtWorks India|E
Nagarro India|E
EPAM India|E
GlobalLogic India|E
Virtusa India|E
Publicis Sapient India|E
Sutherland|E
Hexagon India|E
Zycus|E
Druva|E
Icertis|E
Sprinklr India|E
CleverTap|E
MoEngage|E
WebEngage|E
Gupshup|E
Exotel|E
Yellow.ai|E
Haptik|E
Uniphore|E
Observe.AI|E
Skit.ai|E
Leena AI|E
Ola Krutrim|E
Krutrim|E
Sarvam AI|E
Zluri|E
Whatfix|E
Mindtickle|E
Capillary Technologies|E
Clari India|E
Darwinbox|E
Keka|E
greytHR|E
Zimyo|E
Razorpay X|E
Open Financial|E
Khatabook|E
OkCredit|E
Cashfree Payments|E
Instamojo|E
PayNearby|E
Paytm Money|E
Angel One|R
5paisa|R
Motilal Oswal|R
IIFL|R
Policybazaar|R
Acko|R
Digit Insurance|R
Insurance Dekho|E
Turtlemint|E
Coverfox|E
CoinDCX|E
CoinSwitch|E
WazirX|E
Mudrex|E
Zebpay|E
Ola Financial|E
Dunzo|E
Swiggy Instamart|E
Milkbasket|E
Grofers|E
Curefit|E
HealthifyMe|E
Pristyn Care|E
Innovaccer|E
Healthkart|E
Netmeds|E
1mg|E
MedPlus|E
Mfine|E
Eka Care|E
Qure.ai|E
SigTuple|E
Niramai|E
Tricog|E
Wysa|E
Amaha|E
Dozee|E
Meesho Supply|E
Udaan|E
Zetwerk|E
Jumbotail|E
Moglix|E
IndiaMART|R
Just Dial|R
Zomato Hyperpure|E
Rebel Foods|E
EatSure|E
Box8|E
Dineout|E
EazyDiner|E
Magicpin|E
Paytm Insider|E
BookMyShow|R
Hotstar|E
JioCinema|E
Sony LIV|E
ZEE5|E
MX Player|E
Gaana|E
JioSaavn|E
Pocket FM|E
Kuku FM|E
Sharechat Moj|E
Koo|E
Josh|E
Games24x7|R
Mobile Premier League|E
Winzo|E
Zupee|E
Junglee Games|E
Ludo King (Gametion)|E
Nazara|R
Octro|E
Hike|E
Pratilipi|E
Dunzo Digital|E
Bluestone|R
CaratLane|R
Pepperfry|E
Urban Ladder|E
Wakefit|E
Sleepy Owl|E
The Sleep Company|E
Lenskart Solutions|E
Purplle|E
Mensa Brands|E
GlowRoad|E
Shopsy|E
DealShare|E
Citymall|E
Kirana Club|E
Bijak|E
DeHaat|E
Ninjacart|E
AgroStar|E
Cropin|E
Fasal|E
Gramophone|E
BharatAgri|E
Waycool|E
Ola Cars|E
Vogo|E
Zoomcar|E
Revv|E
Drivezy|E
BluSmart|E
Lithium Urban|E
Chalo|E
Moveinsync|E
Mahindra Logistics|R
Blue Dart|R
Gati|R
Shadowfax|E
Dunzo Daily|E
Loadshare|E
Rivigo|E
BlackBuck|E
Zinka Logistics|E
Vahak|E
Locus.sh|E
LogiNext|E
Fareye|E
Increff|E
Unicommerce|R
Browntape|E
Vinculum|E
Anchanto|E
Easyecom|E
Zoho Books|E
Zoho CRM|E
Tally Solutions|E
ClearTax|E
Cleartax Cleartrip|E
Cleartrip|E
Goibibo|E
Yatra|R
EaseMyTrip|R
Thomas Cook India|R
Treebo|E
FabHotels|E
Zostel|E
StayVista|E
Airbnb India|E
Sterlite Technologies|R
Tejas Networks|R
HFCL|R
Dixon Technologies|R
Amber Enterprises|R
Syrma SGS|R
Kaynes Technology|R
CG Power|R
Cyient DLM|R
Data Patterns|R
Zen Technologies|R
Astra Microwave|R
Paras Defence|R
""",
"small": """
Mapmyindia (MapmyIndia)|R
CE Info Systems|R
Ola Maps|E
Kaleyra|E
Gupshup Technology|E
Pinelabs Plural|E
Aurionpro Solutions|R
Cigniti Technologies|R
Intense Technologies|R
Xchanging Solutions|R
Softsol India|R
Blue Star Infotech|R
Ksolves India|E
Ampere Software|E
Cybage Software|E
Persistent AI|E
Synechron India|E
Zensar AI|E
Quinnox|E
Neilsoft|E
Bahwan CyberTek|E
Vertex Computer Systems|E
Infogain India|E
Altimetrik India|E
Aptean India|E
Mobisoft Infotech|E
Simform|E
Techjockey|E
Hidden Brains|E
Bacancy Technology|E
Openxcell|E
Brainvire|E
Radixweb|E
Azilen Technologies|E
Tatvasoft|E
Space-O Technologies|E
Mindinventory|E
Ideas2IT|E
Chetu India|E
Successive Technologies|E
Appinventiv|E
Konstant Infosolutions|E
Kellton|E
Valuefirst|E
Nitor Infotech|E
Eternus Solutions|E
Calsoft|E
EInfochips|E
Sasken|E
Tessolve|E
Mistral Solutions|E
Ignitarium|E
Wipro Ventures|E
Microchip India|E
Moschip Technologies|R
Sankalp Semiconductor|E
Cientra|E
Alphawave India|E
Mirafra Technologies|E
Bangalore Dynamics|E
Vayavya Labs|E
Softnautics|E
Tata Elxsi Medical|E
Zeta Suite|E
Setu|E
Perfios|E
IDfy|E
Signzy|E
Karza Technologies|E
Digio|E
Decentro|E
Hyperverge|E
Onfido India|E
Yubi (CredAvenue)|E
Progcap|E
Indifi|E
Flexiloans|E
Capital Float|E
Aye Finance|E
Moneyview|E
Money View|E
Fibe (EarlySalary)|E
Stashfin|E
Zest Money|E
Uni Cards|E
OneCard (FPL Technologies)|E
Fi Money|E
Niyo|E
Jar|E
Dhan|E
Smallcase|E
INDmoney|E
Wealthy|E
Scripbox|E
ET Money|E
Kuvera|E
Paytm Payments Bank|E
Airtel Payments Bank|E
Jio Payments Bank|E
Ujjivan SFB|R
AU Small Finance Bank|R
Equitas|R
Bandhan Bank|R
RBL Bank|R
Federal Bank|R
South Indian Bank|R
Karur Vysya Bank|R
IDFC First Bank|R
Piramal Finance|R
Muthoot Finance|R
Manappuram|R
Shriram Finance|R
Mahindra Finance|R
Cholamandalam|R
L&T Finance|R
Poonawalla Fincorp|R
Aditya Birla Capital|R
Tata Capital|R
Tata AIG|R
Bajaj Allianz|R
Star Health|R
Niva Bupa|R
Care Health|R
Max Life|R
PNB MetLife|R
Go Digit|R
Zoho Corporation Pvt|R
Zoho Creator|E
Kissflow|E
Appsmith|E
Hasura|E
Atlan|E
Gojek India|E
Zomato Labs|E
Nutanix India|E
Tessell|E
Rubrik India|E
Cohesity India|E
Zscaler India|E
Quick Heal|R
K7 Computing|E
Seqrite|E
Cyble|E
CloudSEK|E
Sequretek|E
Securden|E
Appknox|E
Safe Security|E
Lucideus|E
Innefu Labs|E
Paladion|E
Subex Ai|E
SISA|E
Indusface|E
Netcore Cloud|E
Netcore Solutions|E
Bluecore India|E
Pubmatic India|E
Vserv|E
Komli|E
Glance|E
Roposo|E
Kuvera Wealth|E
Mad Street Den (Vue.ai)|E
Gramener|E
Fluid AI|E
Active.ai|E
Senseforth|E
Verloop.io|E
Engati|E
Convin|E
Vymo|E
SalesBabu|E
Freshdesk|E
Freshteam|E
Zoho Desk|E
Recruiterflow|E
Hirect|E
Apna|E
Naukri Campus|E
Instahyre|E
Cutshort|E
Hirist|E
Shine.com|E
Foundit (Monster India)|E
Timesjobs|E
WorkIndia|E
Internshala|E
Scaler|E
Newton School|E
Masai School|E
Coding Ninjas|E
GeeksforGeeks|E
Great Learning|E
Imarticus|E
Edureka|E
Intellipaat|E
Knowledgehut|E
Skillsoft India|E
Toppr|E
Doubtnut|E
Embibe|E
Classplus|E
Teachmint|E
Leverage Edu|E
Testbook|E
Adda247|E
Oliveboard|E
Gradeup|E
Byju's Whitehat|E
Lead School|E
Cuemath|E
Vidyalankar|E
Eduvanz|E
Merito|E
Springboard India|E
Zoho Schools|E
""",
"micro": """
Rocketlane|E
Zenduty|E
Hevo Data|E
Atomicwork|E
Spyne|E
Ultrahuman|E
Bugasura|E
Hyperface|E
Lightspeed India|E
Sarvam|E
Gnani.ai|E
Pepper Content|E
Plum|E
Vance|E
Kiwi|E
Zolve|E
Stable Money|E
Ditto Insurance|E
Cuvera|E
Bobble AI|E
Zingbus|E
Pickrr|E
Shipway|E
Shipyaari|E
NimbusPost|E
Vamaship|E
Zippee|E
Wiz Freight|E
FarEye India|E
Cogoport|E
Qualyval|E
Leap Finance|E
Univest|E
Vested|E
IndMoney Global|E
Ola Dash|E
Probo|E
Rooter|E
Loco|E
Skillenza|E
Kratikal|E
Nurix AI|E
Subtl.ai|E
Composio|E
Gupshup AI|E
Zuddl|E
Hashnode|E
Dhiway|E
Navi Technologies|E
Cloudphysician|E
Tarsier|E
Lucid Motors India|E
Pixxel|E
Skyroot|E
Agnikul|E
Digantara|E
Dhruva Space|E
Bellatrix|E
Ananth Technologies|E
Cron AI|E
Sensibull|E
Streak|E
Opstra|E
Tickertape|E
Univest Tech|E
Fyers|E
Dhan HQ|E
Zebu|E
Trading Technologies India|E
AlphaGrep India|E
Graviton Research|E
Tower Research India|E
Quadeye|E
Squarepoint India|E
Dolat Capital|E
NK Securities|E
Jane Street India|E
Zest AI India|E
Jar App|E
Onsurity|E
Loop Health|E
Plum Benefits|E
Even Healthcare|E
Zeno Health|E
Truemeds|E
Pharmarack|E
Wellness Forever|E
Medikabazaar|E
Redcliffe Labs|E
Healthians|E
Orange Health|E
Metropolis|R
Thyrocare|R
Lal PathLabs|R
Neodocs|E
Ultrahuman Health|E
Cure.fit Tech|E
Fittr|E
Sugar.fit|E
Birdy|E
Ayu Health|E
Zyla|E
Even|E
Practo Tech|E
Kapiva|E
Sirona|E
Bombay Shaving|E
The Whole Truth|E
Snitch|E
Bewakoof|E
Souled Store|E
Newme|E
Rare Rabbit|E
Giva|E
Pilgrim|E
Minimalist|E
Plum Goodness|E
Wow Skin Science|E
Perfora|E
Wakao|E
Skippi|E
Open Secret|E
Blinkit Labs|E
Swish|E
Zing|E
Slikk|E
KNOT|E
Snabbit|E
Pronto|E
Urbanpiper|E
Petpooja|E
Posist|E
Dotpe|E
Zomato Ads|E
LimeTray|E
Rista|E
Torqus|E
Vyapar|E
Zoho Invoice|E
Refrens|E
Bizongo|E
Zoho Backstage|E
Vymo Inc|E
SaaSBoomi|E
Spotdraft|E
LegalKart|E
Leegality|E
Vakilsearch|E
IndiaFilings|E
Legalwiz|E
Lawsikho|E
Teamlease Digital|E
Zeta Tech|E
Whatfix Tech|E
Kovai.co|E
Kovai|E
Zoho Learn|E
Tessact|E
Frammer AI|E
Dubverse|E
Neysa|E
Tonic AI|E
Gan.ai|E
Pykih|E
Ignite Solutions|E
Josh Software|E
Joshsoftware|E
Nimblechapps|E
Excellence Technologies|E
Suyati|E
Aress Software|E
Sapphire Software Solutions|E
Tekion India|E
Techahead|E
Dev IT|E
Devsinc India|E
Rapid Innovation|E
Imaginovation India|E
Tudip Technologies|E
Crest Data Systems|E
Crossover India|E
Capermint|E
Thinkitive|E
Itransition India|E
Webdew|E
Cygnet Infotech|E
Cygnet One|E
Elsner Technologies|E
SoftwareONE India|E
""",
}
