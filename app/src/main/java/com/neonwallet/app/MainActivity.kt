package com.neonwallet.app

import android.Manifest
import android.content.pm.PackageManager
import android.net.Uri
import android.graphics.Bitmap
import android.content.Context
import android.provider.ContactsContract
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.*
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.*
import androidx.compose.ui.unit.*
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.text.NumberFormat
import java.util.Locale
import java.util.UUID
import java.net.URLEncoder
import java.math.BigDecimal
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.PBEKeySpec

private val Bg = Color(0xFF090711)
private val Panel = Color(0xFF171323)
private val Purple = Color(0xFF9B5CFF)
private val Lavender = Color(0xFFC7A6FF)
private val Green = Color(0xFF48D7A0)
private val White = Color(0xFFF7F3FF)
private val Muted = Color(0xFFA69BB8)
private const val API_BASE = "https://neon-wallet-api-rpkc.onrender.com"

class MainActivity : ComponentActivity() {
    private var lockAction: (() -> Unit)? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { NeonWalletApp { action -> lockAction = action } }
    }

    override fun onStart() {
        super.onStart()
        // Re-check the lock whenever the activity returns to the foreground.
        Handler(Looper.getMainLooper()).post { lockAction?.invoke() }
    }

    override fun onStop() {
        super.onStop()
        lockAction?.invoke()
    }
}

private data class PhoneContact(val name: String, val number: String)
private data class Recipient(val id: String, val username: String)
private data class Account(val id: String, val username: String, val email: String)
private data class Wallet(val id: String, val balance: Long)
private data class Tx(val id: String, val title: String, val date: String, val amount: Long, val incoming: Boolean)

private object WalletApi {
    private fun call(method: String, path: String, token: String? = null,
                     body: JSONObject? = null, key: String? = null): JSONObject {
        val c = URL(API_BASE + path).openConnection() as HttpURLConnection
        c.requestMethod = method
        c.connectTimeout = 15000
        c.readTimeout = 20000
        c.setRequestProperty("Accept", "application/json")
        if (token != null) c.setRequestProperty("Authorization", "Bearer " + token)
        if (key != null) c.setRequestProperty("Idempotency-Key", key)
        if (body != null) {
            c.doOutput = true
            c.setRequestProperty("Content-Type", "application/json")
            c.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
        }
        try {
            val code = c.responseCode
            val stream = if (code in 200..299) c.inputStream else c.errorStream
            val raw = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
            if (code !in 200..299) {
                val detail = try { JSONObject(raw).optString("detail", "Ошибка запроса: " + code) }
                    catch (_: Exception) { "Ошибка запроса: " + code }
                throw IllegalStateException(detail)
            }
            return if (raw.isBlank()) JSONObject() else JSONObject(raw)
        } finally { c.disconnect() }
    }

    fun login(email: String, password: String): String =
        call("POST", "/v1/auth/login", body = JSONObject().put("email", email).put("password", password)).getString("access_token")

    fun register(name: String, email: String, password: String): String =
        call("POST", "/v1/auth/register", body = JSONObject().put("username", name)
            .put("email", email).put("password", password)).getString("access_token")

    fun load(token: String): Triple<Account, Wallet, List<Tx>> {
        val me = call("GET", "/v1/me", token).getJSONObject("user")
        val account = Account(me.getString("account_id"), me.getString("username"), me.getString("email"))
        val w = call("GET", "/v1/wallet", token)
        val wallet = Wallet(w.getString("account_id"), w.getLong("balance_kopecks"))
        val array = call("GET", "/v1/transactions?limit=100", token).getJSONArray("items")
        val txs = (0 until array.length()).map { i ->
            val t = array.getJSONObject(i)
            val incoming = t.optString("recipient_account_id") == account.id
            Tx(t.getString("id"), t.getString("description"), t.getString("created_at"),
                t.getLong("amount_kopecks"), incoming)
        }
        return Triple(account, wallet, txs)
    }

    fun searchRecipients(token: String, query: String): List<Recipient> {
        val encoded = URLEncoder.encode(query, "UTF-8")
        val array = call("GET", "/v1/recipients/search?q=$encoded", token).getJSONArray("items")
        return (0 until array.length()).map { i ->
            val item = array.getJSONObject(i)
            Recipient(item.getString("account_id"), item.getString("username"))
        }
    }

    fun transfer(token: String, recipient: String, amountKopecks: Long, description: String) =
        call("POST", "/v1/transfers", token,
            JSONObject().put("recipient_account_id", recipient).put("amount_kopecks", amountKopecks)
                .put("description", description.ifBlank { "Перевод NEON WALLET" }),
            UUID.randomUUID().toString())
}

private fun <T> async(task: () -> T, done: (Result<T>) -> Unit) {
    Thread {
        val result = runCatching(task)
        Handler(Looper.getMainLooper()).post { done(result) }
    }.start()
}

@Composable
private fun NeonWalletApp(registerLock: ((() -> Unit) -> Unit)) {
    val context = androidx.compose.ui.platform.LocalContext.current
    val prefs = remember { context.getSharedPreferences("neon_wallet_session", 0) }
    var token by remember { mutableStateOf(prefs.getString("access_token", null)) }
    var account by remember { mutableStateOf<Account?>(null) }
    var wallet by remember { mutableStateOf<Wallet?>(null) }
    var txs by remember { mutableStateOf<List<Tx>>(emptyList()) }
    // A saved session is never considered unlocked after process/activity recreation.
    var page by remember { mutableStateOf(if (token == null) "auth" else "pin") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf("") }
    var tab by remember { mutableIntStateOf(0) }
    var transferOpen by remember { mutableStateOf(false) }
    var ownQrOpen by remember { mutableStateOf(false) }
    var qrRecipient by remember { mutableStateOf("") }
    var recipientResults by remember { mutableStateOf<List<Recipient>>(emptyList()) }
    var recipientSearching by remember { mutableStateOf(false) }
    var recipientSearchError by remember { mutableStateOf("") }
    var phoneContacts by remember { mutableStateOf<List<PhoneContact>>(emptyList()) }
    var contactsAllowed by remember {
        mutableStateOf(ContextCompat.checkSelfPermission(context, Manifest.permission.READ_CONTACTS) == PackageManager.PERMISSION_GRANTED)
    }
    val contactsPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        contactsAllowed = granted
        if (!granted) error = "Доступ к контактам не разрешён."
    }

    val qrScanner = rememberLauncherForActivityResult(ScanContract()) { scanResult ->
        val raw = scanResult.contents.orEmpty()
        if (raw.isNotBlank()) {
            val parsed = runCatching { Uri.parse(raw).getQueryParameter("account") }.getOrNull()
            val candidate = (parsed ?: raw).trim()
            if (candidate.matches(Regex("[0-9]{6,12}"))) {
                qrRecipient = candidate
                transferOpen = true
            } else {
                error = "QR-код не распознан. Нужен QR NEON WALLET с ID получателя."
            }
        }
    }
    val cameraPermission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            qrScanner.launch(ScanOptions().setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                .setPrompt("Наведите камеру на QR-код NEON WALLET")
                .setBeepEnabled(false).setOrientationLocked(false))
        } else error = "Для сканирования QR нужен доступ к камере."
    }
    val startQrScan = {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            qrScanner.launch(ScanOptions().setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                .setPrompt("Наведите камеру на QR-код NEON WALLET")
                .setBeepEnabled(false).setOrientationLocked(false))
        } else cameraPermission.launch(Manifest.permission.CAMERA)
    }

    fun refresh(t: String) {
        busy = true
        async({ WalletApi.load(t) }) { result ->
            busy = false
            result.onSuccess {
                account = it.first
                wallet = it.second
                txs = it.third
                page = "wallet"
                error = ""
            }.onFailure {
                error = it.message ?: "Не удалось загрузить кошелёк"
                if (error.contains("авторизац", true) || error.contains("сессия", true)) {
                    prefs.edit().remove("access_token").apply()
                    token = null
                    page = "auth"
                } else page = "wallet"
            }
        }
    }

    SideEffect {
        registerLock {
            if (token != null && prefs.contains("pin_hash")) {
                page = "pin"
                transferOpen = false
            }
        }
    }

    LaunchedEffect(token, page) {
        if (token == null) page = "auth"
        else if (page == "loading") refresh(token!!)
    }
    LaunchedEffect(contactsAllowed) {
        if (contactsAllowed) {
            async({ readPhoneContacts(context) }) { result ->
                phoneContacts = result.getOrDefault(emptyList())
            }
        }
    }

    MaterialTheme(colorScheme = darkColorScheme(
        background = Bg, surface = Panel, primary = Purple, onPrimary = White,
        onBackground = White, onSurface = White, secondary = Green
    )) {
        Surface(Modifier.fillMaxSize(), color = Bg) {
            when (page) {
                "pin" -> PinScreen(prefs, setup = false, onComplete = { page = "loading" })
                "pin_setup" -> PinScreen(prefs, setup = true, onComplete = { page = "loading" })
                "auth" -> AuthScreen(busy, error) { register, name, email, password ->
                    busy = true
                    error = ""
                    async({
                        if (register) WalletApi.register(name, email, password)
                        else WalletApi.login(email, password)
                    }) { result ->
                        busy = false
                        result.onSuccess {
                            prefs.edit().putString("access_token", it).apply()
                            token = it
                            // Existing users must verify their saved PIN after password login.
                            page = if (prefs.contains("pin_hash")) "pin" else "pin_setup"
                        }.onFailure { error = it.message ?: "Ошибка входа" }
                    }
                }
                "loading" -> LoadingScreen()
                else -> {
                    if (transferOpen) {
                        TransferScreen(
                        balance = wallet?.balance ?: 0L, busy = busy, initialRecipient = qrRecipient,
                        recipients = recipientResults, searching = recipientSearching, searchError = recipientSearchError,
                        scanQr = startQrScan,
                        close = { transferOpen = false; qrRecipient = ""; recipientResults = emptyList() },
                        search = { query ->
                            val current = token
                            if (current != null && query.trim().length >= 3) {
                                recipientSearching = true
                                recipientSearchError = ""
                                async({ WalletApi.searchRecipients(current, query.trim()) }) { result ->
                                    recipientSearching = false
                                    result.onSuccess { recipientResults = it }
                                        .onFailure { recipientSearchError = it.message ?: "Не удалось найти получателя" }
                                }
                            }
                        },
                        send = { recipient, amountKopecks, description ->
                            val current = token ?: return@TransferScreen
                            busy = true
                            async({ WalletApi.transfer(current, recipient, amountKopecks, description) }) { result ->
                                busy = false
                                result.onSuccess {
                                    transferOpen = false
                                    qrRecipient = ""
                                    recipientResults = emptyList()
                                    refresh(current)
                                }.onFailure { error = it.message ?: "Перевод не выполнен" }
                            }
                        }
                    )
                    } else {
                        Column(Modifier.fillMaxSize()) {
                            Box(Modifier.weight(1f)) {
                                when (tab) {
                                    0 -> HomeScreen(account, wallet, txs, busy, { transferOpen = true }, { token?.let { refresh(it) } }, { ownQrOpen = true })
                                    1 -> HistoryScreen(txs)
                                    2 -> ContactsScreen(phoneContacts, contactsAllowed) {
                                        contactsPermission.launch(Manifest.permission.READ_CONTACTS)
                                    }
                                    else -> ProfileScreen(account) {
                                        prefs.edit().remove("access_token").remove("pin_salt").remove("pin_hash").apply()
                                        token = null
                                        account = null
                                        wallet = null
                                        txs = emptyList()
                                        page = "auth"
                                    }
                                }
                            }
                            BottomBar(tab) { tab = it }
                        }
                    }
                    if (ownQrOpen) account?.let { currentAccount ->
                        MyQrDialog(currentAccount.id) { ownQrOpen = false }
                    }
                    if (error.isNotBlank()) AlertDialog(
                        onDismissRequest = { error = "" }, containerColor = Panel,
                        title = { Text("NEON WALLET", color = White) },
                        text = { Text(error, color = Muted) },
                        confirmButton = { TextButton(onClick = { error = "" }) { Text("Понятно", color = Lavender) } }
                    )
                }
            }
        }
    }
}

@Composable
private fun AuthScreen(busy: Boolean, error: String,
                       submit: (Boolean, String, String, String) -> Unit) {
    var register by remember { mutableStateOf(false) }
    var name by remember { mutableStateOf("") }
    var email by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    Box(Modifier.fillMaxSize().background(Brush.verticalGradient(listOf(Color(0xFF241638), Bg, Bg))),
        contentAlignment = Alignment.Center) {
        Column(Modifier.fillMaxWidth().padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            BrandMark(60)
            Spacer(Modifier.height(22.dp))
            Text(if (register) "Создать аккаунт" else "С возвращением",
                color = White, fontSize = 27.sp, fontWeight = FontWeight.Bold)
            Text("Твой виртуальный кошелёк", color = Muted, fontSize = 13.sp,
                modifier = Modifier.padding(top = 6.dp, bottom = 24.dp))
            Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(24.dp))
                .background(Panel).border(1.dp, Purple.copy(alpha = .25f), RoundedCornerShape(24.dp)).padding(18.dp)) {
                if (register) {
                    Field("Имя пользователя", name, { name = it }, Icons.Default.Person)
                    Spacer(Modifier.height(12.dp))
                }
                Field("Электронная почта", email, { email = it }, Icons.Default.Email, KeyboardType.Email)
                Spacer(Modifier.height(12.dp))
                Field("Пароль", password, { password = it }, Icons.Default.Lock, KeyboardType.Password, true)
                if (error.isNotBlank()) Text(error, color = Color(0xFFFF8585), fontSize = 12.sp,
                    modifier = Modifier.padding(top = 10.dp))
                Spacer(Modifier.height(17.dp))
                Button(onClick = { submit(register, name.trim(), email.trim(), password) },
                    enabled = !busy && email.isNotBlank() && password.isNotBlank() && (!register || name.length >= 3),
                    modifier = Modifier.fillMaxWidth().height(52.dp), shape = RoundedCornerShape(15.dp),
                    colors = ButtonDefaults.buttonColors(containerColor = Purple)) {
                    if (busy) CircularProgressIndicator(Modifier.size(20.dp), color = White, strokeWidth = 2.dp)
                    else Text(if (register) "Зарегистрироваться" else "Войти", fontWeight = FontWeight.Bold)
                }
                TextButton(onClick = { register = !register }) {
                    Text(if (register) "Уже есть аккаунт? Войти" else "Создать аккаунт", color = Lavender)
                }
            }
            Text("Виртуальные средства · без банковских платежей", color = Muted, fontSize = 10.sp,
                modifier = Modifier.padding(top = 18.dp))
        }
    }
}

@Composable
private fun HomeScreen(account: Account?, wallet: Wallet?, txs: List<Tx>, busy: Boolean,
                       transfer: () -> Unit, refresh: () -> Unit, myQr: () -> Unit) {
    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 24.dp)) {
        item {
            Row(Modifier.fillMaxWidth().padding(22.dp), verticalAlignment = Alignment.CenterVertically) {
                BrandMark(46)
                Spacer(Modifier.width(12.dp))
                Column {
                    Text("NEON WALLET", color = White, fontWeight = FontWeight.Black, fontSize = 16.sp, letterSpacing = 1.4.sp)
                    Text("ТВОИ ФИНАНСЫ ПОД КОНТРОЛЕМ", color = Muted, fontSize = 9.sp, letterSpacing = 1.sp)
                }
                Spacer(Modifier.weight(1f))
                IconButton(onClick = refresh) {
                    if (busy) CircularProgressIndicator(Modifier.size(19.dp), color = Lavender, strokeWidth = 2.dp)
                    else Icon(Icons.Default.Refresh, null, tint = Lavender)
                }
            }
        }
        item {
            Column(Modifier.padding(horizontal = 20.dp)) {
                Text("МОЙ КОШЕЛЁК", color = Muted, fontSize = 10.sp, letterSpacing = 2.sp, fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.height(10.dp))
                Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(28.dp))
                    .background(Brush.linearGradient(listOf(Color(0xFF422A5D), Color(0xFF21182F), Panel)))
                    .border(1.dp, Purple.copy(alpha = .35f), RoundedCornerShape(28.dp)).padding(22.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("Доступный баланс", color = Color(0xFFD4C8E5), fontSize = 13.sp)
                        Spacer(Modifier.weight(1f))
                        Text("АКТИВЕН", color = Green, fontSize = 9.sp, fontWeight = FontWeight.Bold)
                    }
                    Text(wallet?.let { money(it.balance) } ?: "— ₽", color = White,
                        fontSize = 36.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(top = 10.dp))
                    Text("Виртуальные RUB", color = Muted, fontSize = 11.sp, modifier = Modifier.padding(top = 4.dp))
                    Spacer(Modifier.height(15.dp))
                    Text("ID аккаунта: " + (account?.id ?: "—"), color = Lavender, fontSize = 12.sp)
                    Spacer(Modifier.height(18.dp))
                    Button(onClick = transfer, modifier = Modifier.fillMaxWidth().height(50.dp),
                        shape = RoundedCornerShape(15.dp), colors = ButtonDefaults.buttonColors(containerColor = Purple)) {
                        Icon(Icons.Default.ArrowUpward, null)
                        Spacer(Modifier.width(8.dp))
                        Text("Перевести средства", fontWeight = FontWeight.Bold)
                    }
                    Spacer(Modifier.height(9.dp))
                    OutlinedButton(onClick = myQr, modifier = Modifier.fillMaxWidth().height(48.dp),
                        shape = RoundedCornerShape(15.dp), border = androidx.compose.foundation.BorderStroke(1.dp, Lavender.copy(alpha = .65f))) {
                        Icon(Icons.Default.QrCode2, null, tint = Lavender)
                        Spacer(Modifier.width(8.dp))
                        Text("Мой QR-код", color = Lavender)
                    }
                }
            }
        }
        item {
            Row(Modifier.fillMaxWidth().padding(horizontal = 22.dp).padding(top = 27.dp, bottom = 13.dp),
                verticalAlignment = Alignment.CenterVertically) {
                Text("Последние операции", color = White, fontSize = 18.sp, fontWeight = FontWeight.Bold)
                Spacer(Modifier.weight(1f))
                Text(txs.size.toString(), color = Muted, fontSize = 12.sp)
            }
        }
        if (txs.isEmpty()) item { EmptyState("Пока нет операций", "История переводов появится здесь") }
        else items(txs.take(5), key = { it.id }) { TransactionRow(it) }
    }
}

@Composable
private fun MyQrDialog(accountId: String, close: () -> Unit) {
    val bitmap = remember(accountId) {
        runCatching {
            val matrix = QRCodeWriter().encode(
                "neonwallet://pay?account=$accountId", BarcodeFormat.QR_CODE, 640, 640
            )
            Bitmap.createBitmap(640, 640, Bitmap.Config.ARGB_8888).apply {
                for (x in 0 until 640) for (y in 0 until 640) {
                    setPixel(x, y, if (matrix[x, y]) android.graphics.Color.BLACK else android.graphics.Color.WHITE)
                }
            }
        }.getOrNull()
    }
    AlertDialog(
        onDismissRequest = close, containerColor = Panel,
        title = { Text("Мой QR-код", color = White, fontWeight = FontWeight.Bold) },
        text = {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                if (bitmap != null) {
                    Image(bitmap.asImageBitmap(), contentDescription = "QR-код NEON WALLET",
                        modifier = Modifier.fillMaxWidth().padding(8.dp).clip(RoundedCornerShape(12.dp)))
                }
                Text("ID аккаунта: $accountId", color = Lavender, fontSize = 12.sp)
                Text("Другой пользователь может отсканировать код для перевода виртуальных средств.",
                    color = Muted, fontSize = 11.sp, modifier = Modifier.padding(top = 8.dp))
            }
        },
        confirmButton = { TextButton(onClick = close) { Text("Готово", color = Lavender) } }
    )
}

@Composable
private fun HistoryScreen(txs: List<Tx>) {
    Column(Modifier.fillMaxSize()) {
        Column(Modifier.padding(22.dp)) {
            Text("История", color = White, fontSize = 27.sp, fontWeight = FontWeight.Bold)
            Text("Все операции кошелька", color = Muted, fontSize = 12.sp, modifier = Modifier.padding(top = 5.dp))
        }
        if (txs.isEmpty()) EmptyState("Операций пока нет", "Движения средств появятся здесь")
        else LazyColumn(contentPadding = PaddingValues(bottom = 24.dp)) {
            items(txs, key = { it.id }) { TransactionRow(it) }
        }
    }
}

@Composable
private fun ContactsScreen(contacts: List<PhoneContact>, permissionGranted: Boolean, requestPermission: () -> Unit) {
    Column(Modifier.fillMaxSize()) {
        Column(Modifier.padding(22.dp)) {
            Text("Контакты", color = White, fontSize = 27.sp, fontWeight = FontWeight.Bold)
            Text("Люди из телефонной книги", color = Muted, fontSize = 12.sp,
                modifier = Modifier.padding(top = 5.dp))
        }
        if (!permissionGranted) {
            Column(Modifier.fillMaxWidth().padding(20.dp).clip(RoundedCornerShape(22.dp))
                .background(Panel).padding(20.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                Icon(Icons.Default.Contacts, null, tint = Lavender, modifier = Modifier.size(36.dp))
                Text("Разреши доступ к контактам", color = White, fontWeight = FontWeight.Bold,
                    modifier = Modifier.padding(top = 12.dp))
                Text("Контакты будут прочитаны только на устройстве и не отправляются на сервер.",
                    color = Muted, fontSize = 12.sp, modifier = Modifier.padding(top = 6.dp))
                Button(onClick = requestPermission, modifier = Modifier.fillMaxWidth().padding(top = 14.dp),
                    colors = ButtonDefaults.buttonColors(containerColor = Purple)) {
                    Text("Разрешить доступ")
                }
            }
        } else if (contacts.isEmpty()) {
            EmptyState("Контакты не найдены", "В телефонной книге пока нет сохранённых номеров")
        } else {
            LazyColumn(contentPadding = PaddingValues(bottom = 20.dp)) {
                items(contacts, key = { it.number }) { contact ->
                    Row(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 4.dp)
                        .clip(RoundedCornerShape(15.dp)).background(Panel).padding(13.dp),
                        verticalAlignment = Alignment.CenterVertically) {
                        Box(Modifier.size(42.dp).clip(CircleShape).background(Purple.copy(alpha = .2f)),
                            contentAlignment = Alignment.Center) {
                            Text(contact.name.take(1).uppercase(), color = Lavender, fontWeight = FontWeight.Bold)
                        }
                        Spacer(Modifier.width(12.dp))
                        Column {
                            Text(contact.name, color = White, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                            Text(contact.number, color = Muted, fontSize = 11.sp)
                        }
                    }
                }
            }
        }
        Text("Для виртуального перевода нужен ID NEON WALLET или QR-код получателя.",
            color = Muted, fontSize = 10.sp, modifier = Modifier.padding(horizontal = 22.dp, vertical = 8.dp))
    }
}

private fun readPhoneContacts(context: Context): List<PhoneContact> {
    val result = mutableListOf<PhoneContact>()
    val projection = arrayOf(
        ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
        ContactsContract.CommonDataKinds.Phone.NUMBER
    )
    context.contentResolver.query(
        ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
        projection, null, null,
        ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " COLLATE LOCALIZED ASC"
    )?.use { cursor ->
        val nameIndex = cursor.getColumnIndex(ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME)
        val numberIndex = cursor.getColumnIndex(ContactsContract.CommonDataKinds.Phone.NUMBER)
        while (cursor.moveToNext()) {
            val name = if (nameIndex >= 0) cursor.getString(nameIndex).orEmpty() else ""
            val number = if (numberIndex >= 0) cursor.getString(numberIndex).orEmpty() else ""
            if (name.isNotBlank() && number.isNotBlank()) result.add(PhoneContact(name, number))
        }
    }
    return result.distinctBy { it.number }.take(500)
}

@Composable
private fun ProfileScreen(account: Account?, logout: () -> Unit) {
    Column(Modifier.fillMaxSize().padding(22.dp)) {
        Text("Профиль", color = White, fontSize = 27.sp, fontWeight = FontWeight.Bold)
        Text("Настройки аккаунта", color = Muted, fontSize = 12.sp, modifier = Modifier.padding(top = 5.dp, bottom = 22.dp))
        Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(22.dp)).background(Panel).padding(18.dp),
            verticalAlignment = Alignment.CenterVertically) {
            BrandMark(54)
            Spacer(Modifier.width(14.dp))
            Column {
                Text(account?.username ?: "NEON USER", color = White, fontWeight = FontWeight.Bold, fontSize = 16.sp)
                Text(account?.email ?: "", color = Muted, fontSize = 11.sp)
                Text("ID " + (account?.id ?: "—"), color = Lavender, fontSize = 11.sp, modifier = Modifier.padding(top = 5.dp))
            }
        }
        Spacer(Modifier.height(14.dp))
        ProfileRow(Icons.Default.Security, "Безопасность", "Защищённая серверная авторизация")
        ProfileRow(Icons.Default.Storage, "Данные кошелька", "Баланс и операции хранятся на сервере")
        ProfileRow(Icons.Default.Info, "О приложении", "NEON WALLET")
        Spacer(Modifier.weight(1f))
        OutlinedButton(onClick = logout, modifier = Modifier.fillMaxWidth().height(50.dp),
            border = androidx.compose.foundation.BorderStroke(1.dp, Color(0xFF765A91)), shape = RoundedCornerShape(15.dp)) {
            Icon(Icons.Default.Logout, null, tint = Lavender)
            Spacer(Modifier.width(8.dp))
            Text("Выйти из аккаунта", color = Lavender)
        }
    }
}

@Composable
private fun ProfileRow(icon: androidx.compose.ui.graphics.vector.ImageVector, title: String, subtitle: String) {
    Row(Modifier.fillMaxWidth().padding(vertical = 5.dp).clip(RoundedCornerShape(16.dp))
        .background(Panel).padding(15.dp), verticalAlignment = Alignment.CenterVertically) {
        Icon(icon, null, tint = Lavender)
        Spacer(Modifier.width(13.dp))
        Column {
            Text(title, color = White, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
            Text(subtitle, color = Muted, fontSize = 10.sp)
        }
    }
}

@Composable
private fun TransferScreen(
    balance: Long, busy: Boolean, initialRecipient: String,
    recipients: List<Recipient>, searching: Boolean, searchError: String,
    scanQr: () -> Unit, close: () -> Unit, search: (String) -> Unit,
    send: (String, Long, String) -> Unit
) {
    var query by remember { mutableStateOf(initialRecipient) }
    var selected by remember { mutableStateOf<Recipient?>(null) }
    var amount by remember { mutableStateOf("") }
    var description by remember { mutableStateOf("Перевод NEON WALLET") }
    var review by remember { mutableStateOf(false) }
    var localError by remember { mutableStateOf("") }
    LaunchedEffect(initialRecipient) {
        if (initialRecipient.isNotBlank()) {
            query = initialRecipient
            selected = Recipient(initialRecipient, "Получатель по QR")
        }
    }
    val amountKopecks = runCatching {
        BigDecimal(amount.replace(',', '.')).movePointRight(2).longValueExact()
    }.getOrNull() ?: 0L
    val valid = selected != null && amountKopecks > 0 && amountKopecks <= balance &&
        amountKopecks <= 100_000_000L

    Column(Modifier.fillMaxSize().background(Bg).navigationBarsPadding()) {
        Row(Modifier.fillMaxWidth().background(Color(0xFF100C18)).statusBarsPadding()
            .padding(horizontal = 16.dp, vertical = 13.dp), verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = { if (review) review = false else close() }) {
                Icon(Icons.Default.ArrowBack, "Назад", tint = Lavender)
            }
            Column(Modifier.weight(1f)) {
                Text(if (review) "Подтверждение" else "Новый перевод", color = White, fontSize = 19.sp, fontWeight = FontWeight.Bold)
                Text("Перевод внутри NEON WALLET", color = Muted, fontSize = 11.sp)
            }
            BrandMark(36)
        }
        if (!review) {
            Column(Modifier.weight(1f).fillMaxWidth().verticalScroll(rememberScrollState()).padding(horizontal = 20.dp, vertical = 16.dp)) {
                Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(22.dp))
                    .background(Brush.linearGradient(listOf(Color(0xFF422A5D), Color(0xFF21182F), Panel)))
                    .border(1.dp, Purple.copy(alpha = .3f), RoundedCornerShape(22.dp)).padding(18.dp)) {
                    Text("ДОСТУПНО ДЛЯ ПЕРЕВОДА", color = Color(0xFFD4C8E5), fontSize = 10.sp, letterSpacing = 1.4.sp)
                    Text(money(balance), color = White, fontSize = 29.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(top = 5.dp))
                    Text("Виртуальный баланс", color = Muted, fontSize = 11.sp)
                }
                Text("ПОЛУЧАТЕЛЬ", color = Muted, fontSize = 10.sp, letterSpacing = 1.5.sp, fontWeight = FontWeight.Bold,
                    modifier = Modifier.padding(top = 24.dp, bottom = 10.dp))
                Field("ID аккаунта или имя пользователя", query, { query = it.trim().take(32); selected = null; localError = "" }, Icons.Default.Person)
                Row(Modifier.fillMaxWidth().padding(top = 9.dp), horizontalArrangement = Arrangement.spacedBy(9.dp)) {
                    Button(onClick = {
                        if (query.matches(Regex("[0-9]{6,12}"))) selected = Recipient(query, "Получатель по ID")
                        else search(query)
                    }, enabled = query.trim().length >= 3 && !searching,
                        modifier = Modifier.weight(1f).height(48.dp), shape = RoundedCornerShape(14.dp),
                        colors = ButtonDefaults.buttonColors(containerColor = Purple)) {
                        if (searching) CircularProgressIndicator(Modifier.size(18.dp), color = White, strokeWidth = 2.dp)
                        else Icon(Icons.Default.Search, null)
                        Spacer(Modifier.width(7.dp)); Text("Найти")
                    }
                    OutlinedButton(onClick = scanQr, modifier = Modifier.weight(1f).height(48.dp),
                        shape = RoundedCornerShape(14.dp), border = androidx.compose.foundation.BorderStroke(1.dp, Lavender.copy(alpha = .55f))) {
                        Icon(Icons.Default.QrCodeScanner, null, tint = Lavender); Spacer(Modifier.width(7.dp)); Text("QR-код", color = Lavender)
                    }
                }
                if (searchError.isNotBlank()) Text(searchError, color = Color(0xFFFF8585), fontSize = 12.sp, modifier = Modifier.padding(top = 8.dp))
                if (recipients.isNotEmpty()) {
                    Text("НАЙДЕННЫЕ АККАУНТЫ", color = Muted, fontSize = 10.sp, letterSpacing = 1.2.sp, modifier = Modifier.padding(top = 17.dp, bottom = 7.dp))
                    recipients.forEach { recipient ->
                        Row(Modifier.fillMaxWidth().padding(vertical = 4.dp).clip(RoundedCornerShape(15.dp))
                            .background(if (selected?.id == recipient.id) Color(0xFF30203F) else Panel)
                            .border(1.dp, if (selected?.id == recipient.id) Purple else Color.Transparent, RoundedCornerShape(15.dp))
                            .clickable { selected = recipient; query = recipient.id }.padding(13.dp), verticalAlignment = Alignment.CenterVertically) {
                            Box(Modifier.size(42.dp).clip(CircleShape).background(Purple.copy(alpha = .2f)), contentAlignment = Alignment.Center) {
                                Text(recipient.username.take(1).uppercase(), color = Lavender, fontWeight = FontWeight.Bold)
                            }
                            Spacer(Modifier.width(11.dp))
                            Column(Modifier.weight(1f)) {
                                Text(recipient.username, color = White, fontWeight = FontWeight.SemiBold, fontSize = 13.sp)
                                Text("ID " + recipient.id, color = Muted, fontSize = 10.sp)
                            }
                            Icon(if (selected?.id == recipient.id) Icons.Default.CheckCircle else Icons.Default.ChevronRight, null, tint = Lavender)
                        }
                    }
                }
                selected?.let { recipient ->
                    Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(Color(0xFF182A27)).padding(12.dp),
                        verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Default.CheckCircle, null, tint = Green); Spacer(Modifier.width(8.dp))
                        Column {
                            Text("Получатель выбран", color = Green, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                            Text(recipient.username + " · " + recipient.id, color = White, fontSize = 12.sp)
                        }
                    }
                }
                Text("СУММА ПЕРЕВОДА", color = Muted, fontSize = 10.sp, letterSpacing = 1.5.sp, fontWeight = FontWeight.Bold,
                    modifier = Modifier.padding(top = 24.dp, bottom = 10.dp))
                OutlinedTextField(value = amount, onValueChange = { value ->
                    val normalized = value.replace(',', '.')
                    if (normalized.matches(Regex("[0-9]{0,8}(\\.[0-9]{0,2})?"))) amount = normalized
                }, modifier = Modifier.fillMaxWidth(), label = { Text("Сумма, ₽") }, singleLine = true,
                    textStyle = androidx.compose.ui.text.TextStyle(color = White, fontSize = 25.sp, fontWeight = FontWeight.Bold),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                    leadingIcon = { Icon(Icons.Default.CurrencyRuble, null, tint = Lavender) },
                    colors = OutlinedTextFieldDefaults.colors(focusedTextColor = White, unfocusedTextColor = White,
                        focusedBorderColor = Purple, unfocusedBorderColor = Color(0xFF51445F),
                        focusedLabelColor = Lavender, unfocusedLabelColor = Muted, cursorColor = Lavender))
                Row(Modifier.fillMaxWidth().padding(top = 9.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    listOf("100", "500", "1 000", "5 000").forEach { chip ->
                        Surface(onClick = { amount = chip.replace(" ", "") }, shape = RoundedCornerShape(12.dp), color = Panel,
                            modifier = Modifier.weight(1f)) {
                            Box(Modifier.padding(vertical = 11.dp), contentAlignment = Alignment.Center) {
                                Text(chip, color = Lavender, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
                            }
                        }
                    }
                }
                Text("КОММЕНТАРИЙ", color = Muted, fontSize = 10.sp, letterSpacing = 1.5.sp, fontWeight = FontWeight.Bold,
                    modifier = Modifier.padding(top = 22.dp, bottom = 10.dp))
                Field("За что перевод?", description, { description = it.take(140) }, Icons.Default.Edit)
                if (amount.isNotBlank() && amountKopecks > balance) Text("Недостаточно средств на балансе", color = Color(0xFFFF8585), fontSize = 12.sp,
                    modifier = Modifier.padding(top = 9.dp))
                if (localError.isNotBlank()) Text(localError, color = Color(0xFFFF8585), fontSize = 12.sp)
                Text("Виртуальные средства переводятся только между аккаунтами NEON WALLET.", color = Muted, fontSize = 10.sp,
                    modifier = Modifier.padding(top = 18.dp))
            }
            Column(Modifier.fillMaxWidth().background(Color(0xFF100C18)).padding(16.dp)) {
                Button(onClick = {
                    if (selected == null) localError = "Сначала найди и выбери получателя."
                    else if (amountKopecks <= 0) localError = "Укажи сумму перевода."
                    else if (amountKopecks > balance) localError = "Недостаточно виртуальных средств."
                    else review = true
                }, enabled = valid && !busy, modifier = Modifier.fillMaxWidth().height(54.dp),
                    shape = RoundedCornerShape(16.dp), colors = ButtonDefaults.buttonColors(containerColor = Purple)) {
                    Text("Продолжить", fontWeight = FontWeight.Bold, fontSize = 15.sp); Spacer(Modifier.width(8.dp)); Icon(Icons.Default.ArrowForward, null)
                }
            }
        } else {
            Column(Modifier.weight(1f).fillMaxWidth().verticalScroll(rememberScrollState()).padding(20.dp),
                horizontalAlignment = Alignment.CenterHorizontally) {
                Box(Modifier.padding(top = 22.dp).size(82.dp).clip(CircleShape).background(Purple.copy(alpha = .17f)),
                    contentAlignment = Alignment.Center) { Icon(Icons.Default.Send, null, tint = Lavender, modifier = Modifier.size(36.dp)) }
                Text("Проверь перевод", color = White, fontSize = 23.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(top = 17.dp))
                Text("Перед отправкой убедись, что всё верно", color = Muted, fontSize = 12.sp, modifier = Modifier.padding(top = 5.dp))
                Text(money(amountKopecks), color = White, fontSize = 36.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(top = 28.dp, bottom = 20.dp))
                Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(20.dp)).background(Panel).padding(17.dp)) {
                    ReviewLine("Получатель", selected?.username ?: "—")
                    ReviewLine("ID аккаунта", selected?.id ?: "—")
                    Divider(color = Color(0xFF34283F), modifier = Modifier.padding(vertical = 12.dp))
                    ReviewLine("Комментарий", description.ifBlank { "Перевод NEON WALLET" })
                    ReviewLine("Комиссия", "0 ₽")
                    ReviewLine("Итого", money(amountKopecks), true)
                }
                Text("После подтверждения виртуальные средства будут списаны с твоего баланса.", color = Muted, fontSize = 11.sp,
                    modifier = Modifier.padding(top = 18.dp))
            }
            Column(Modifier.fillMaxWidth().background(Color(0xFF100C18)).padding(16.dp)) {
                Button(onClick = { selected?.let { send(it.id, amountKopecks, description) } }, enabled = valid && !busy,
                    modifier = Modifier.fillMaxWidth().height(54.dp), shape = RoundedCornerShape(16.dp),
                    colors = ButtonDefaults.buttonColors(containerColor = Purple)) {
                    if (busy) CircularProgressIndicator(Modifier.size(19.dp), color = White, strokeWidth = 2.dp)
                    else { Text("Подтвердить перевод", fontWeight = FontWeight.Bold, fontSize = 15.sp); Spacer(Modifier.width(8.dp)); Icon(Icons.Default.Check, null) }
                }
                TextButton(onClick = { review = false }, modifier = Modifier.fillMaxWidth()) { Text("Изменить данные", color = Lavender) }
            }
        }
    }
}

@Composable
private fun ReviewLine(label: String, value: String, strong: Boolean = false) {
    Row(Modifier.fillMaxWidth().padding(vertical = 5.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(label, color = Muted, fontSize = 12.sp, modifier = Modifier.weight(1f))
        Text(value, color = if (strong) Lavender else White, fontSize = 12.sp, fontWeight = if (strong) FontWeight.Bold else FontWeight.Medium)
    }
}

@Composable
private fun Field(label: String, value: String, change: (String) -> Unit,
                  icon: androidx.compose.ui.graphics.vector.ImageVector,
                  keyboard: KeyboardType = KeyboardType.Text, password: Boolean = false) {
    OutlinedTextField(value = value, onValueChange = change, modifier = Modifier.fillMaxWidth(),
        label = { Text(label) }, singleLine = true,
        leadingIcon = { Icon(icon, null, tint = Muted, modifier = Modifier.size(18.dp)) },
        visualTransformation = if (password) PasswordVisualTransformation() else VisualTransformation.None,
        keyboardOptions = KeyboardOptions(keyboardType = keyboard),
        colors = OutlinedTextFieldDefaults.colors(focusedTextColor = White, unfocusedTextColor = White,
            focusedBorderColor = Purple, unfocusedBorderColor = Color(0xFF51445F),
            focusedLabelColor = Lavender, unfocusedLabelColor = Muted, cursorColor = Lavender))
}

@Composable
private fun TransactionRow(tx: Tx) {
    Row(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 5.dp)
        .clip(RoundedCornerShape(16.dp)).background(Panel).padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(42.dp).clip(RoundedCornerShape(14.dp))
            .background(if (tx.incoming) Green.copy(alpha = .13f) else Purple.copy(alpha = .16f)),
            contentAlignment = Alignment.Center) {
            Icon(if (tx.incoming) Icons.Default.ArrowDownward else Icons.Default.ArrowUpward, null,
                tint = if (tx.incoming) Green else Lavender)
        }
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(tx.title, color = White, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
            Text(tx.date.replace("T", " ").take(16), color = Muted, fontSize = 10.sp)
        }
        Text((if (tx.incoming) "+" else "−") + money(tx.amount), color = if (tx.incoming) Green else White,
            fontSize = 12.sp, fontWeight = FontWeight.Bold)
    }
}

@Composable
private fun EmptyState(title: String, subtitle: String) {
    Column(Modifier.fillMaxWidth().padding(35.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Icon(Icons.Default.ReceiptLong, null, tint = Lavender, modifier = Modifier.size(38.dp))
        Text(title, color = White, fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(top = 12.dp))
        Text(subtitle, color = Muted, fontSize = 11.sp, modifier = Modifier.padding(top = 5.dp))
    }
}

@Composable
private fun LoadingScreen() {
    Box(Modifier.fillMaxSize().background(Bg), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            BrandMark(62)
            CircularProgressIndicator(Modifier.padding(top = 22.dp).size(25.dp), color = Lavender, strokeWidth = 2.dp)
            Text("Подключаем кошелёк…", color = Muted, fontSize = 12.sp, modifier = Modifier.padding(top = 14.dp))
        }
    }
}

@Composable
private fun BrandMark(size: Int = 52) {
    Box(Modifier.size(size.dp).clip(RoundedCornerShape((size / 3).dp))
        .background(Brush.linearGradient(listOf(Color(0xFFBD8BFF), Purple, Color(0xFF6230A8)))),
        contentAlignment = Alignment.Center) {
        Icon(Icons.Default.Bolt, null, tint = Color(0xFF160D22), modifier = Modifier.size((size * .62f).dp))
    }
}

@Composable
private fun BottomBar(selected: Int, select: (Int) -> Unit) {
    Row(Modifier.fillMaxWidth().background(Color(0xFF100C18)).navigationBarsPadding()
        .padding(horizontal = 10.dp, vertical = 8.dp), horizontalArrangement = Arrangement.SpaceAround) {
        val entries = listOf(Triple(Icons.Default.Home, "Главная", 0),
            Triple(Icons.Default.ReceiptLong, "История", 1), Triple(Icons.Default.Contacts, "Контакты", 2),
            Triple(Icons.Default.Person, "Профиль", 3))
        entries.forEach { entry ->
            Column(Modifier.weight(1f).clip(RoundedCornerShape(13.dp)).clickable { select(entry.third) }
                .padding(vertical = 7.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                Icon(entry.first, null, tint = if (selected == entry.third) Lavender else Muted, modifier = Modifier.size(21.dp))
                Text(entry.second, color = if (selected == entry.third) Lavender else Muted, fontSize = 10.sp,
                    fontWeight = if (selected == entry.third) FontWeight.Bold else FontWeight.Normal)
            }
        }
    }
}

private fun money(kopecks: Long): String =
    NumberFormat.getNumberInstance(Locale("ru", "RU")).format(kopecks / 100.0) + " ₽"


private fun derivePinHash(pin: String, salt: ByteArray): ByteArray {
    val spec = PBEKeySpec(pin.toCharArray(), salt, 150_000, 256)
    return try {
        SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(spec).encoded
    } finally {
        spec.clearPassword()
    }
}

private fun storePin(prefs: android.content.SharedPreferences, pin: String) {
    val salt = ByteArray(16).also { SecureRandom().nextBytes(it) }
    val hash = derivePinHash(pin, salt)
    prefs.edit()
        .putString("pin_salt", Base64.getEncoder().encodeToString(salt))
        .putString("pin_hash", Base64.getEncoder().encodeToString(hash))
        .apply()
}

private fun verifyPin(prefs: android.content.SharedPreferences, pin: String): Boolean {
    return try {
        val salt = Base64.getDecoder().decode(prefs.getString("pin_salt", null) ?: return false)
        val expected = Base64.getDecoder().decode(prefs.getString("pin_hash", null) ?: return false)
        MessageDigest.isEqual(expected, derivePinHash(pin, salt))
    } catch (_: Exception) {
        false
    }
}

@Composable
private fun PinScreen(
    prefs: android.content.SharedPreferences,
    setup: Boolean,
    onComplete: () -> Unit
) {
    var pin by remember { mutableStateOf("") }
    var confirmation by remember { mutableStateOf("") }
    var error by remember { mutableStateOf("") }
    val hasPin = prefs.contains("pin_hash")
    val isSetup = setup || !hasPin

    Box(
        Modifier.fillMaxSize().background(
            Brush.verticalGradient(listOf(Color(0xFF241638), Bg, Bg))
        ),
        contentAlignment = Alignment.Center
    ) {
        Column(
            Modifier.fillMaxWidth().padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            BrandMark(64)
            Spacer(Modifier.height(24.dp))
            Text(
                if (isSetup) "Создай PIN-код" else "Введи PIN-код",
                color = White, fontSize = 25.sp, fontWeight = FontWeight.Bold
            )
            Text(
                if (isSetup) "Он понадобится при каждом новом входе в приложение"
                else "Защита доступа к NEON WALLET",
                color = Muted, fontSize = 12.sp,
                modifier = Modifier.padding(top = 8.dp, bottom = 22.dp)
            )
            Column(
                Modifier.fillMaxWidth().clip(RoundedCornerShape(24.dp))
                    .background(Panel)
                    .border(1.dp, Purple.copy(alpha = .25f), RoundedCornerShape(24.dp))
                    .padding(20.dp)
            ) {
                OutlinedTextField(
                    value = pin,
                    onValueChange = { value ->
                        if (value.length <= 6 && value.all(Char::isDigit)) {
                            pin = value
                            error = ""
                        }
                    },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("6 цифр") },
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = White, unfocusedTextColor = White,
                        focusedBorderColor = Purple, unfocusedBorderColor = Color(0xFF51445F),
                        focusedLabelColor = Lavender, unfocusedLabelColor = Muted,
                        cursorColor = Lavender
                    )
                )
                if (isSetup) {
                    Spacer(Modifier.height(12.dp))
                    OutlinedTextField(
                        value = confirmation,
                        onValueChange = { value ->
                            if (value.length <= 6 && value.all(Char::isDigit)) {
                                confirmation = value
                                error = ""
                            }
                        },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("Повтори PIN-код") },
                        singleLine = true,
                        visualTransformation = PasswordVisualTransformation(),
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedTextColor = White, unfocusedTextColor = White,
                            focusedBorderColor = Purple, unfocusedBorderColor = Color(0xFF51445F),
                            focusedLabelColor = Lavender, unfocusedLabelColor = Muted,
                            cursorColor = Lavender
                        )
                    )
                }
                if (error.isNotBlank()) {
                    Text(error, color = Color(0xFFFF8585), fontSize = 12.sp,
                        modifier = Modifier.padding(top = 10.dp))
                }
                Spacer(Modifier.height(18.dp))
                Button(
                    onClick = {
                        if (pin.length != 6) {
                            error = "PIN должен состоять из 6 цифр"
                        } else if (isSetup) {
                            if (pin != confirmation) error = "PIN-коды не совпадают"
                            else {
                                storePin(prefs, pin)
                                onComplete()
                            }
                        } else if (verifyPin(prefs, pin)) {
                            onComplete()
                        } else {
                            pin = ""
                            error = "Неверный PIN-код"
                        }
                    },
                    enabled = pin.length == 6 && (!isSetup || confirmation.length == 6),
                    modifier = Modifier.fillMaxWidth().height(52.dp),
                    shape = RoundedCornerShape(15.dp),
                    colors = ButtonDefaults.buttonColors(containerColor = Purple)
                ) {
                    Text(if (isSetup) "Сохранить PIN" else "Разблокировать",
                        fontWeight = FontWeight.Bold)
                }
            }
            Text("PIN хранится только на этом устройстве", color = Muted, fontSize = 10.sp,
                modifier = Modifier.padding(top = 18.dp))
        }
    }
}
