# تشغيل البوت ٢٤ ساعة مجاناً على Oracle Cloud ☁️

الخطة دي مجانية للأبد (Always Free) ومفيش فيها أي خصم من الكارت.

## الخطوة 1: اعمل حساب Oracle المجاني

1. افتح [oracle.com/cloud/free](https://www.oracle.com/cloud/free/) ودوس **Start for free**.
2. اكتب اسمك وإيميلك واختار بلدك، وافتح الإيميل ودوس لينك التأكيد.
3. **Home Region**: اختار **Germany Central (Frankfurt)** — سيرفرات ARM المجانية بتبقى فاضية فيها أكتر. (تنبيه: الاختيار ده مش بيتغير بعد كده.)
4. هيطلب **كارت بنكي للتحقق من الهوية بس** — مش بيخصم حاجة (ممكن حجز مؤقت بيرجع خلال 3-5 أيام). لو الكارت اترفض، جرّب كارت تاني: الكروت المدفوعة مقدماً والافتراضية Oracle بترفضها.

## الخطوة 2: اعمل السيرفر (VM)

1. من القائمة ☰ اختار **Compute → Instances** وبعدين **Create instance**.
2. الاسم: `musicbot`
3. تحت **Image and shape** دوس **Edit**:
   - **Image**: Canonical Ubuntu (نسخة 24.04, aarch64)
   - **Shape → Change shape → Ampere (ARM-based processor)** → اختار **VM.Standard.A1.Flex** وخليها **2 OCPU و 12 GB RAM** (ده الحد المجاني للأبد)
4. **Networking**: سيبها تعمل VCN جديد، وتأكد إن **Assign a public IPv4 address** متعلم عليها ✅
5. **SSH keys**: اختار **Generate a key pair for me** ودوس **Download private key** — احفظ الملف في Downloads (اسمه هيبقى حاجة زي `ssh-key-....key`).
6. دوس **Create**.
   - لو ظهر **Out of host capacity**: غيّر الـ Availability Domain من **Placement** وجرب تاني، أو استنى ساعة وعيد — دي مشكلة شائعة ومؤقتة ومش غلطتك.
7. لما الحالة تبقى **RUNNING** (أخضر)، انسخ الـ **Public IPv4 address** من صفحة السيرفر.

## الخطوة 3: أمرين بس من PowerShell

افتح **PowerShell** (هتكتب الأوامر من أي مكان، الأوامر فيها المسارات كاملة).

لو أول أمر اشتكى من صلاحيات المفتاح (permissions too open)، نفّذ ده مرة واحدة الأول:

```
icacls "$env:USERPROFILE\Downloads\ssh-key-2026-11-27.key" /inheritance:r /grant:r "$env:USERNAME:R"
```
(غيّر اسم الملف لاسم المفتاح اللي نزّلته)

**الأمر الأول** — يرفع ملف التنصيب للسيرفر (حط الـ IP بتاعك مكان `IP`):

```
scp -i "$env:USERPROFILE\Downloads\ssh-key-2026-11-27.key" "$env:USERPROFILE\Downloads\setup.sh" ubuntu@IP:
```

**الأمر التاني** — ينصّب ويشغّل كل حاجة لوحدها (5-10 دقايق):

```
ssh -i "$env:USERPROFILE\Downloads\ssh-key-2026-11-27.key" ubuntu@IP "bash setup.sh"
```

في الآخر لازم تشوف **active (running)** بالأخضر = البوت شغال ٢٤ ساعة ✅

## بعد كده

- البوت بيرجع يشتغل لوحده حتى لو السيرفر اتعمله ريستارت.
- لو عايز تشوف اللوج في أي وقت: `ssh -i "المفتاح" ubuntu@IP "journalctl -u musicbot -n 50"` 
- الرصيد التجريبي ($300) اللي هتلاقيه في الحساب مالوش لازمة — سيرفر Always Free مش بيستخدمه ومش بيتحاسب.
- **ميزة تشغيل المكالمات الصوتية**: لما تحبها، شغّل `python gen_session.py` على اللاب توب، سجل دخول بالحساب التاني، وابعتلي النص اللي يطلعلك وأنا أضيفه للسيرفر في دقيقة.
