# Support Bridge — Teknik Dokümantasyon

**Modüller:** `support_bridge_hub`, `support_bridge_client`
**Sürüm:** 19.0.1.1.0 · **Odoo:** 19.0 · **Geliştirici:** CodeQuarters

---

## 1. Ürün ne yapıyor?

Bir Odoo iş ortağının (partner/bayi) kendi Odoo'su ile müşterilerinin **ayrı ayrı, birbirinden bağımsız** Odoo kurulumları arasında canlı bir destek sohbeti kurar. Sohbet, özel bir arayüz değil **Odoo'nun kendi Discuss uygulaması** üzerinden yürür.

İki taraflı bir ürün:

| Modül | Kimde kurulu | Ne görür |
|---|---|---|
| `support_bridge_hub` | Destek **veren** firma (bayi) | "Destek" kanalı altında her müşteri için ayrı bir alt kanal |
| `support_bridge_client` | Destek **alan** firma (müşteri) | Tedarikçisinin adını taşıyan tek bir kanal |

Müşteri kendi Odoo'sunda mesaj yazar, mesaj bayinin Odoo'sundaki o müşteriye ait kanala düşer. Bayi cevap yazar, cevap müşterinin kanalına düşer. İki taraf da alışık olduğu Discuss arayüzünü kullanır; ek yazılım, portal veya üçüncü parti servis yoktur.

### Neden değerli?

Bugün bayiler müşteri desteğini e-posta, WhatsApp veya ayrı bir helpdesk aracıyla yürütüyor. Bunların hiçbiri Odoo'nun içinde değil; kayıt tutulmuyor, kim ne demiş takip edilemiyor, müşteri her seferinde başka bir mecraya geçmek zorunda kalıyor. Support Bridge, desteği her iki tarafın da zaten günlük kullandığı yere (Discuss) taşır.

---

## 2. Temel mimari kararlar

Bu bölüm sunumda en çok soru gelecek kısım. Her kararın arkasında somut bir kısıt var.

### 2.1. Tüm bağlantıları müşteri başlatır (NAT güvenliği)

**Problem:** Müşterilerin Odoo'su çoğu zaman şirket içi sunucuda, güvenlik duvarı/NAT arkasında çalışır. Dışarıdan o sunucuya erişilemez.

**Karar:** Sistem, bayinin müşteriye ulaşabildiğini **hiçbir zaman varsaymaz**. Bütün zorunlu trafik müşteriden bayiye doğrudur:

- Müşteri → Bayi: mesaj yazıldığı anda HTTP ile gönderilir.
- Bayi → Müşteri: müşteri her dakika bayiye "bana yeni bir şey var mı?" diye sorar (cron).

Bu tasarım sayesinde müşterinin sabit IP'si, alan adı veya açık portu olmasına gerek yoktur. Kurulum, API anahtarını yapıştırıp "Bağlan" demekten ibarettir.

### 2.2. İsteğe bağlı anlık teslimat (push)

Periyodik kontrol güvenli ama yavaştır (en fazla ~1 dakika gecikme). Müşterinin Odoo'su bulutta ve internetten erişilebilir durumdaysa bu gecikmeye gerek yok.

**Karar:** Müşteri isterse "Genel Erişilebilir" seçeneğini açıp kendi adresini girer. Bu adres bağlanma anında bayiye bildirilir; bayi bundan sonra cevapları doğrudan müşteriye gönderir ve mesaj anında düşer.

Kritik nokta: **push bir hızlandırıcıdır, bir bağımlılık değil.** Periyodik kontrol her durumda çalışmaya devam eder. Push başarısız olursa (ağ kesintisi, sunucu yeniden başlıyor) mesaj kaybolmaz, en geç bir dakika içinde periyodik kontrolle gelir. Bu yüzden push kodunda yeniden deneme mekanizması yoktur — olması gereksiz karmaşıklık olurdu.

### 2.3. Neden long-polling / websocket kullanmadık?

Long-polling, her bekleyen müşteri için sunucuda bir işçi (worker) sürecini meşgul eder. 8 işçiniz varsa ve 10 müşteri 50 saniyelik bekleme yapıyorsa Odoo'nuz kilitlenir. Odoo'nun kendi canlı bildirim sistemi bu sorunu ayrı bir gevent süreci ve PostgreSQL `LISTEN/NOTIFY` ile çözer; ama bu altyapıyı iki ayrı veritabanı arasında kurmak, bir App Store modülünün üstlenemeyeceği bir operasyon yüküdür (ek süreç, ek port, ek yapılandırma).

**Karar:** Kısa aralıklı push + periyodik kontrol. Push zaten anlık olduğu için long-polling'in getireceği fayda yok; getireceği maliyet ise çok yüksek.

### 2.4. Kimlik: müşteri başına API anahtarı

Her müşteri kaydı oluşturulduğunda 32 baytlık rastgele bir anahtar üretilir (`secrets.token_urlsafe`). Bu anahtar sadece bir parola değil, **müşterinin kimliğidir** — bayi gelen isteğin hangi müşteriden geldiğini bu anahtardan anlar.

Ortak tek anahtar kullanılsaydı, müşteriyi ayırt etmek için isteğe bir müşteri numarası eklemek gerekirdi; o numarayı da müşterinin kendisi gönderdiği için herkes başkasının numarasını yazıp onun sohbetine girebilirdi. Yani tek anahtar pratikte kimlik doğrulamayı ortadan kaldırırdı.

### 2.5. Mesaj yazarları: gerçek kontaklar

Karşı taraftaki kişilerin bu Odoo'da kullanıcı hesabı yoktur. Mesajın içine "Ali dedi ki:" yazmak yerine, her kişi için gerçek bir `res.partner` kontağı açılır ve mesaj o kontağın adına yazılır.

Yapı iki katmanlıdır:
- **Temsil kontağı** (persona): karşı firmayı temsil eder, `is_company=True`.
- **Kişi kontakları**: temsil kontağının altına açılır (`parent_id`).

Sonuç: Discuss'ta mesajlar Odoo'nun standart "Firma, Kişi" biçiminde görünür — örneğin *"CodeQuarters, Mitchell Admin"*. Bu hem doğal görünür hem de yankı önleme mekanizmasının temelini oluşturur.

**Eşleştirme anahtarı addır değil, karşı taraftaki kontak kimliğidir.** Her mesajla birlikte gönderenin kendi `res.partner` id'si de taşınır ve kontaklar bu kimlikle eşleştirilir. Ad yalnızca görünen etikettir; karşı tarafta değiştiğinde burada da güncellenir. Ada göre eşleştirmek iki hataya yol açardı: aynı adı taşıyan iki kişi tek kontağa düşerdi, ve adını değiştiren bir kişi ikinci bir kontak açtırıp geçmişini bölerdi. E-posta da taşınır ama yalnızca ayırt edici bilgi olarak — asla eşleştirme anahtarı değildir, çünkü değişebilir ve paylaşılan kutular birden çok kişiye ait olabilir.

### 2.6. Kanal tipi: `group` (güvenlik açısından kritik)

Odoo'da `channel_type='channel'` seçilirse, çekirdeğin erişim kuralı ([mail/security/mail_security.xml](../odoo-source/addons/mail/security/mail_security.xml), `ir_rule_discuss_channel_all`) **üyeliğe hiç bakmaz**; erişimi `group_public_id` alanı üzerinden verir ve bu alan varsayılan olarak *tüm dahili kullanıcıları* kapsar. Yani her çalışan, üye olmasa bile bütün müşteri sohbetlerini okuyabilirdi.

`channel_type='group'` seçildiğinde erişim yalnızca üyeliğe bağlıdır (kanalın kendisine veya üst kanalına üye olmak). Bu yüzden her iki modül de `group` tipi kullanır.

> **Bu, geliştirme sırasında test edilerek bulunmuş gerçek bir açıktı.** Üye olmayan sıradan bir kullanıcı hesabıyla yapılan denemede kanaldaki 7 mesajın tamamı okunabiliyordu; düzeltmeden sonra aynı test erişim reddi verdi.

### 2.7. Destek ekibi tek havuzdur (bilinmesi gereken sınır)

Odoo çekirdeği, bir alt kanala eklenen herkesi **otomatik olarak üst kanala da üye yapar** (`discuss.channel.member.create` içinde, "member list should be kept in sync" gerekçesiyle). Erişim kuralı ise üst kanal üyeliğini tüm alt kanallar için geçerli sayar.

Pratik sonucu şudur:

- Hiçbir müşteriye temsilci atanmamış bir çalışan **hiçbir destek kanalını göremez**. (Asıl korunmak istenen şey budur ve sağlanmıştır.)
- Herhangi bir müşteriye temsilci olarak atanan bir çalışan, **diğer müşterilerin kanallarını da görebilir**. Yani temsilciler tek bir destek ekibi havuzu oluşturur, müşteri bazında birbirinden yalıtılmazlar.
- Bir kişiyi bütün müşterilerin temsilci listesinden çıkardığınızda erişimi **tamamen** kesilir; modül bu durumda kişiyi üst kanaldan da çıkarır.

Müşteri bazında katı yalıtım gerekiyorsa tek yol, alt kanal yapısından vazgeçip her müşteri kanalını bağımsız bir kanal yapmaktır. Bu, Discuss'taki "Destek → müşteri" gruplamasını kaybettirir; mevcut sürüm gruplamayı tercih etmiştir.

---

## 3. Mesaj akışı adım adım

### 3.1. Müşteri → Bayi

1. Kullanıcı, müşterinin Odoo'sunda kanala mesaj yazar.
2. `mail.message.create` kancası devreye girer, mesajın köprülenmiş bir kanala ait olduğunu görür.
3. Bir **giden kuyruk** (`support.bridge.outbox`) satırı oluşturulur.
4. Transaction commit edildikten **sonra**, arka plan iş parçacığında bayinin `/support_bridge/inbound` adresine HTTP isteği atılır.
5. Bayi anahtarı doğrular, yazar adı için kontak bulur/oluşturur, mesajı ilgili kanala yazar.
6. Bayi, oluşan mesajın kimliğini geri döner; müşteri bunu eşleme tablosuna kaydeder (tepkiler için gerekli).

Gönderim başarısız olursa satır `failed` olur ve yeniden deneme cron'u devralır (5 denemeye kadar).

### 3.2. Bayi → Müşteri

1. Temsilci, bayinin Odoo'sunda müşteri kanalına cevap yazar.
2. `mail.message.create` kancası bir **olay** (`support.bridge.event`) kaydı oluşturur.
3. Müşteri push açıksa, commit sonrası arka plan iş parçacığında müşterinin `/support_bridge/deliver` adresine gönderilir → anında düşer.
4. Push kapalıysa (veya başarısız olduysa) müşterinin cron'u dakikada bir `/support_bridge/outbound` adresini sorar ve olayları imleçten (cursor) itibaren çeker.

Her iki yol da aynı olay kuyruğunu okur; bu yüzden mesaj asla iki kez yazılmaz (bkz. 4.3).

### 3.3. Yankı (echo) önleme

En kritik mantık budur. Köprülenen bir mesaj karşı tarafa yazıldığında, orada yine `mail.message.create` kancası tetiklenir — önlem alınmazsa mesaj sonsuz döngüde iki taraf arasında gidip gelirdi.

**Kural:** İçeri aktarılan mesajlar her zaman *karşı tarafın temsil kontağı* ya da onun altındaki bir kişi kontağı adına yazılır. Her iki modül de gönderim öncesi bu kontrolü yapar (`_is_remote_author`) ve bu kontaklara ait mesajları dışarı göndermez.

Kontrol hem temsil kontağını hem de `parent_id`'si o kontak olan alt kontakları kapsar — sadece temsil kontağına bakmak, kişi kontakları eklendikten sonra döngüye yol açardı.

---

## 4. Veri modeli

### Hub tarafı (`support_bridge_hub`)

**`support.bridge.customer`** — Bir müşteri bağlantısı.

| Alan | Açıklama |
|---|---|
| `name` | Müşteri adı. İlk bağlantıda müşterinin gerçek şirket adıyla otomatik değişir. |
| `api_key` | Otomatik üretilen kimlik anahtarı. Sadece sistem yöneticisi görür. |
| `partner_id` | Müşteriyi temsil eden kontak (otomatik). |
| `channel_id` | Müşteriye özel Discuss alt kanalı (otomatik). |
| `agent_user_ids` | Bu kanalı görebilecek/yanıtlayabilecek dahili kullanıcılar. |
| `client_public_url` | Müşterinin push adresi. Bağlantı anında müşteriden öğrenilir. |
| `last_seen` | Müşterinin sunucuya en son ulaştığı an. |
| `active` | Arşivlenirse müşterinin erişimi kesilir, sohbet geçmişi durur. |

**`support.bridge.event`** — Müşteriye iletilecek olaylar kuyruğu. Tipi `message`, `reaction_add` veya `reaction_remove`. 30 gün sonra otomatik temizlenir (sohbet mesajları değil, yalnızca kuyruk kayıtları).

**`res.company`** üzerine eklenen `support_bridge_parent_channel_id` — tüm müşteri kanallarının altında toplandığı "Destek" ana kanalı.

### Client tarafı (`support_bridge_client`)

**`support.bridge.connection`** — Tedarikçiye bağlantı.

| Alan | Açıklama |
|---|---|
| `hub_url` | Tedarikçinin sunucu adresi. |
| `api_key` | Tedarikçinin verdiği anahtar. |
| `state` | Bağlı değil / Bağlı / Bağlantı hatası. |
| `partner_id`, `channel_id` | Tedarikçi kontağı ve kanal (otomatik). |
| `member_user_ids` | Kanalı görebilecek dahili kullanıcılar. |
| `push_enabled`, `public_url` | Anlık teslimat ayarları. |
| `last_poll_cursor` | En son işlenen olay numarası. Mükerrer teslimatı engeller. |

**`support.bridge.outbox`** — Giden mesaj kuyruğu. Tedarikçiye ulaşıp ulaşmadığının kaydı. `sent` satırlar 30 gün sonra silinir; `failed` satırlar kalıcı tutulur (dışarı çıkamayan mesajın kanıtı). **Sohbet mesajlarının kendisi `mail.message`'ta durur ve asla silinmez.**

**`support.bridge.message.map`** — Yerel mesaj ↔ tedarikçi tarafındaki mesaj eşlemesi. İki işi var: tepkinin karşı tarafta doğru mesaja uygulanmasını sağlar, ve push ile periyodik kontrol yarıştığında mükerrer kaydı veritabanı seviyesinde engeller.

---

## 5. HTTP uç noktaları

Tümü hub tarafında yayınlanır, `Authorization: Bearer <api_key>` başlığıyla korunur.

| Uç nokta | Yön | İş |
|---|---|---|
| `POST /support_bridge/ping` | Müşteri → Bayi | Bağlantı testi. Müşteri şirket adını ve varsa push adresini bildirir; karşılığında bayinin adını alır. |
| `POST /support_bridge/inbound` | Müşteri → Bayi | Mesaj (metin ve/veya ekler) gönderir. |
| `POST /support_bridge/reaction` | Müşteri → Bayi | Emoji tepkisi ekler/kaldırır. |
| `GET /support_bridge/outbound?since=N` | Müşteri → Bayi | N numarasından sonraki olayları çeker. |

Client tarafında ise tek bir uç nokta vardır:

| Uç nokta | Yön | İş |
|---|---|---|
| `POST /support_bridge/deliver` | Bayi → Müşteri | Anlık teslimat. Yalnızca "Genel Erişilebilir" açıkken kabul edilir. |

---

## 6. Kod haritası — hangi dosya ne yapıyor?

### `support_bridge_hub/`

**`models/support_bridge_customer.py`** — Modülün kalbi.

- `create()` → yeni müşteri kaydında kontağı ve kanalı otomatik oluşturur.
- `_ensure_partner_and_channel()` → temsil kontağını ve alt kanalı kurar.
- `_get_or_create_parent_channel()` → "Destek" ana kanalını bulur ya da oluşturup şirkete kaydeder.
- `_update_remote_name()` / `_update_public_url()` → her bağlantıda müşteriden öğrenilenleri işler.
- `_enqueue_event()` → giden olayı kuyruğa yazar ve push'u tetikler.
- `_serialize_event()` / `_serialize_attachments()` / `_decode_attachments()` → ağ biçimi dönüşümleri.
- `_push_to_client()` → commit sonrası, ayrı iş parçacığında anlık gönderim.
- `_get_or_create_remote_author()` → gönderen kişi için kontak bulur/oluşturur.
- `_is_remote_author()` → yankı önleme kontrolü.
- `_sync_channel_members()` → temsilci listesi değişince kanal üyeliklerini günceller.

**`models/mail_message.py`** — Kanala yazılan her mesajı yakalayıp olay kuyruğuna ekler (kendi ilettiklerimiz hariç).

**`models/mail_message_reaction.py`** — Emoji tepkilerini yakalar; ekleme ve kaldırmayı ayrı olay olarak kuyruğa yazar.

**`models/support_bridge_event.py`** — Olay kuyruğu modeli ve 30 günlük otomatik temizlik.

**`controllers/main.py`** — Dört HTTP uç noktası ve anahtar doğrulama.

### `support_bridge_client/`

**`models/support_bridge_connection.py`** — Modülün kalbi.

- `action_connect()` → "Bağlan" butonu. Sunucuyu doğrular, kontağı ve kanalı oluşturur.
- `send_message()` / `send_reaction()` → giden istekler.
- `_deliver_one()` → gelen bir olayı işler (push ve periyodik kontrol aynı yolu kullanır).
- `_deliver_message()` / `_deliver_reaction()` → olay tipine göre işleme.
- `_poll_one()` / `_cron_poll_all()` → periyodik kontrol.
- `_get_or_create_remote_author()`, `_is_remote_author()` → hub tarafındaki muadilleriyle aynı işi yapar.

**`models/support_bridge_outbox.py`** — Giden kuyruk, yeniden deneme mantığı ve temizlik.

**`models/mail_message.py`** — Yazılan mesajı kuyruğa alır, commit sonrası arka planda gönderir.

**`models/mail_message_reaction.py`** — Tepkileri karşı tarafa iletir.

**`models/support_bridge_message_map.py`** — Mesaj eşleme tablosu.

**`controllers/main.py`** — Anlık teslimat uç noktası.

---

## 7. Dayanıklılık: ne olursa ne olur?

Bu bölüm "peki şu olursa?" sorularının cevabı.

**Tedarikçi sunucusu kapalıyken müşteri mesaj yazarsa?**
Mesaj yerel olarak normal şekilde kaydedilir, kullanıcı hiçbir hata görmez. Giden kuyruk satırı `failed` olur ve yeniden deneme cron'u 5 kez dener. Sunucu geri geldiğinde mesaj iletilir.

**Mesaj yazarken karşı taraf yavaşsa kullanıcı bekler mi?**
Hayır. Tüm dış HTTP çağrıları transaction commit edildikten **sonra**, ayrı bir arka plan iş parçacığında yapılır. Kullanıcının "gönder" tuşu anında döner. Bu aynı zamanda şunu garanti eder: yalnızca gerçekten kaydedilmiş veri karşıya gider, geri alınan (rollback) bir işlem karşı tarafta hayalet mesaj bırakmaz.

**Kuyrukta bozuk bir olay olursa tüm sistem durur mu?**
Hayır. Her olay kendi kayıt noktasında (savepoint) işlenir; işlenemeyen olay loglanır, imleç ilerletilir ve sıradakine geçilir. Tek bir bozuk kayıt kuyruğu kilitleyemez.

**Push ve periyodik kontrol aynı anda aynı mesajı getirirse?**
Getiremez. Mesaj eşleme tablosundaki benzersizlik kısıtı ikinci kaydı veritabanı seviyesinde reddeder. Ayrıca imleç mantığı zaten işlenmiş olayları atlar.

**Tedarikçi hatalı bir istek reddederse (HTTP 400 gibi)?**
Bu kalıcı bir hatadır; yeniden denemek fayda etmez. Sistem bunu anlar ve deneme hakkını tek seferde tüketir, boşuna sunucuyu zorlamaz.

**Sunucu tam mesaj gönderilirken yeniden başlarsa?**
Arka plan iş parçacığı kaybolur ama kuyruk satırı `pending` durumda kalır. Cron, 5 dakikadan eski `pending` satırları da toplar ve gönderir.

---

## 8. Kurulum ve yapılandırma

### Bayi tarafı
1. `support_bridge_hub` modülünü kur.
2. **Ayarlar → Teknik → Destek Merkezi → Müşteri Bağlantıları** menüsünden yeni kayıt aç.
3. Müşteri adını yaz, bu kanalı görecek temsilcileri seç, kaydet.
4. Otomatik üretilen API anahtarını ve sunucu adresini müşteriye ilet.

### Müşteri tarafı
1. `support_bridge_client` modülünü kur.
2. **Ayarlar → Teknik → Tedarikçi Desteği** menüsüne git.
3. Hub URL ve API anahtarını yapıştır, ekibi seç.
4. Odoo'n internetten erişilebiliyorsa "Genel Erişilebilir"i aç ve kendi adresini gir (isteğe bağlı, anlık teslimat için).
5. **Bağlan**'a tıkla. Kontak ve kanal otomatik oluşur.

### Geliştirme ortamı (bu projede)

| Adres | Rol | Veritabanı | Şirket |
|---|---|---|---|
| localhost:8069 | Hub | `odoo19` | CodeQuarters |
| localhost:8072 | Müşteri 1 | `client1` | Alfa Lojistik |
| localhost:8073 | Müşteri 2 | `client2` | Beta Tekstil |

Container'lar birbirini servis adıyla ve **container içi 8069 portundan** görür: `http://odoo:8069`, `http://odoo-c1:8069`, `http://odoo-c2:8069`. Dışarıdaki 8072/8073 portları yalnızca tarayıcıdan erişim içindir.

---

## 9. Olası sorular ve cevapları

### Ürün / iş tarafı

**S: Bu Odoo'nun kendi Helpdesk modülünden farkı ne?**
Helpdesk tek bir veritabanı içinde çalışır; müşterinizin kendi Odoo'suyla konuşmaz. Support Bridge iki ayrı, birbirinden habersiz Odoo kurulumunu birbirine bağlar. Ayrıca Helpdesk bir Enterprise modülüdür; Support Bridge Community üzerinde de çalışır.

**S: Neden WhatsApp veya e-posta yerine bunu kullanayım?**
Çünkü konuşma Odoo'nun içinde kalır: aranabilir, kayıtlı, ekleriyle birlikte, ilgili kişiye atfedilmiş halde. Ayrıca müşteri tarafında da aynı düzen vardır — her iki taraf da kendi sisteminde geçmişi görür.

**S: Müşterinin ayrı bir modül kurması gerekmesi engel değil mi?**
Client modülü ücretsiz konumlandırılıyor ve kurulumu bir API anahtarı yapıştırmaktan ibaret. Karşılığında müşteri, desteğe kendi Odoo'sundan erişiyor.

**S: Kaç müşteriye kadar ölçeklenir?**
Push açık müşterilerde sunucu yükü yok denecek kadar azdır (mesaj başına tek bir HTTP isteği). Push kapalı müşterilerde her müşteri dakikada bir kısa istek atar; 50 müşteri dakikada 50 hafif istek demektir ki bu ihmal edilebilir bir yüktür. Long-polling seçseydik bu sayı sunucuyu kilitlerdi.

### Güvenlik

**S: Neden tek bir API anahtarı kullanmıyoruz?**
Anahtar bir parola değil, kimliktir. Tek anahtarla hub müşterileri ayırt edemez; ayırt etmek için istekteki bir müşteri numarasına güvenmek gerekir ki bunu müşterinin kendisi gönderdiği için taklit edilebilir. Ayrıca tek anahtar sızarsa tüm portföy açılır, ve bir müşterinin erişimini kesmek için tüm müşterilerin anahtarını değiştirmek gerekir.

**S: Anahtar müşterinin veritabanında duruyor, risk değil mi?**
Alan yalnızca sistem yöneticisi grubuna görünür. Sızma durumunda etki tek müşteriyle sınırlıdır ve o kaydı arşivleyerek erişim anında kesilir.

**S: Bir çalışan başka müşterinin sohbetini görebilir mi?**
Destek ekibinde olmayan bir çalışan hiçbir şey göremez — kanallar `group` tipindedir ve erişim yalnızca üyeliğe bağlıdır. Bu, üye olmayan bir test kullanıcısıyla doğrulanmıştır. Ancak temsilciler kendi aralarında yalıtılmaz: herhangi bir müşteriye atanan temsilci, diğer müşterilerin kanallarını da görebilir (Odoo alt kanal üyelerini üst kanala otomatik eklediği için). Destek ekibi tek bir havuzdur; müşteri bazında yalıtım isteniyorsa alt kanal yapısından vazgeçmek gerekir. Ayrıntı için bölüm 2.7.

**S: Bir temsilcinin erişimini nasıl keserim?**
Onu ilgili müşterilerin temsilci listesinden çıkarın. Bütün müşterilerden çıkarıldığında modül kişiyi üst kanaldan da çıkarır ve erişimi tamamen kesilir. Bu, silinen üyelikten sonra ayrı bir süreçte erişim denenerek doğrulanmıştır.

**S: Trafik şifreli mi?**
Bayi sunucusu HTTPS ile yayınlandığında tüm trafik TLS ile korunur — modül standart HTTP istemcisi kullandığı için ek yapılandırma gerekmez.

### Teknik

**S: Mesajlar neden anında değil de bir dakikada geliyor?**
Yalnızca push kapalıyken öyle. Müşteri internetten erişilebiliyorsa "Genel Erişilebilir"i açtığında teslimat anlıktır. Bir dakikalık gecikme, NAT arkasındaki müşteriler için ödenen bilinçli bedeldir.

**S: Aynı mesaj iki kez düşer mi?**
Hayır. Olay numarası (imleç) ve mesaj eşleme tablosundaki benzersizlik kısıtı iki katmanlı koruma sağlar.

**S: Sonsuz döngü riski var mı?**
Hayır. İçeri aktarılan mesajlar karşı tarafın temsil kontağı adına yazılır ve her iki modül de bu kontaklara ait mesajları dışarı göndermez.

**S: Dosya ve emoji desteği var mı?**
Evet, ikisi de çift yönlü. Ekler dosya başına 20 MB, mesaj başına 20 dosya ile sınırlıdır. Emoji tepkileri ekleme ve kaldırma olarak senkronize olur.

**S: Sınırı aşan bir dosya gönderirsem ne olur?**
Sessizce kaybolmaz. Mesajın kendisi gider, sınırı aşan ek gitmez ve **her iki taraf da bunu görür**: gönderene kendi kanalında hangi dosyanın neden iletilmediğini söyleyen bir not düşer (indirme bağlantısı gönderme önerisiyle), alıcının gördüğü mesaja da hangi ekin gelmediğini belirten bir satır eklenir. Aynısı mesaj başına 20 dosya sınırı için de geçerlidir.

**S: Karşı tarafta aynı adı taşıyan iki kişi varsa karışır mı?**
Hayır. Kontaklar ada göre değil, karşı taraftaki kontak kimliğine göre eşleştirilir. Aynı adlı iki kişi iki ayrı kontak olur; adını değiştiren kişi ise aynı kontak olarak kalır, yalnızca adı güncellenir ve geçmiş mesajları da yeni adıyla görünür.

**S: Odoo 17/18 desteği var mı?**
Şu an 19 için geliştirildi. 17/18'e taşınırken iki noktaya dikkat edilmeli: alt kanal (`parent_channel_id`) özelliği Odoo 19 ile geldi, ve tepki uygulayan çekirdek fonksiyonun imzası sürümler arasında değişebiliyor.

**S: Veritabanı büyür mü?**
Olay kuyruğu 30 gün, gönderilmiş kuyruk kayıtları 30 gün sonra otomatik temizlenir. Sohbet mesajları Odoo'nun normal mesaj tablosunda kalır ve silinmez.

---

## 10. Bilinen sınırlar ve yol haritası

Bu sürümde **bilinçli olarak** kapsam dışı bırakılanlar:

- **Yazıyor... göstergesi ve çevrimiçi durumu** — iki veritabanı arasında sürekli durum senkronu gerektirir, faydası maliyetini karşılamıyor.
- **Mesaj düzenleme/silme senkronu** — mevcut sürümde gönderilen mesaj karşı tarafta kalır.
- **Bir müşterinin birden fazla tedarikçiye bağlanması** — şu an bağlantı başına tek tedarikçi.
- **Self-servis kayıt** — müşteri kaydını şu an bayi elle açıyor. Ölçek büyüdüğünde davet bağlantısıyla otomatik kayıt eklenebilir.
- **Otomatik testler** — mevcut doğrulama gerçek iki veritabanı arasında uçtan uca yapılıyor; birim testleri henüz yazılmadı.

---

*Bu doküman `support_bridge_hub` ve `support_bridge_client` modüllerinin 19.0.1.1.0 sürümünü anlatır.*
