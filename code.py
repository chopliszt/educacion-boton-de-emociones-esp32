# ============================================================
#  Botón de Emociones — Botón real (ESP32) → n8n (Fila 1)
# ============================================================
#  Este archivo es code.py y vive en la placa.
#  La placa lo ejecuta sola al encender y cada vez que lo guardas.
#  Las credenciales del WiFi NO están aquí: están en settings.toml.
#
#  Historia de este archivo:
#    1. La placa se conecta al WiFi (una sola vez, al arrancar).
#    2. Queda esperando a que alguien presione el botón.
#    3. Cada presión = un POST al webhook de la Fila 1.
#    4. n8n suma 1 en la Data Table. El sitio web pregunta con GET.
#    5. Para https usamos certificate pinning: la placa solo acepta
#       a GTS Root R4, la RAÍZ oficial de Google (válida hasta 2036),
#       que firma a WE1, que firma a *.app.n8n.cloud.

import os
import time
import rtc
import gc
import board
import digitalio
import wifi
import socketpool
import ssl
import adafruit_requests

# ---------------- Configuración: cambia solo esto ----------------

url_del_webhook_fila_1 = "https://TU-CUENTA.app.n8n.cloud/webhook/TU-PATH-DE-FILA-1"

# Pines según el código del profe de robótica (Ideaboard):
pin_donde_esta_el_boton = board.IO27
pines_de_los_leds = [board.IO18, board.IO23, board.IO25]

# ---------------- El reloj: el bug de los 30 años ----------------
# Detalle rarísimo de esta placa: el reloj de CircuitPython cuenta
# segundos desde el año 2000, pero el TLS (mbedTLS) los cuenta
# desde 1970. El TLS lee la fecha 30 años en el pasado y todos los
# certificados le parecen "aún no nacidos". Solución: declarar el
# año real + 30 (el TLS lee 2056 - 30 = 2026 y todo está vigente).
# Si usas esto cuando ya hayan pasado semanas, sube el año de esta
# línea para que el TLS siga viendo la fecha real.

reloj_interno = rtc.RTC()
reloj_interno.datetime = time.struct_time((2056, 11, 10, 16, 30, 0, 1, 314, -1))
print("Fecha declarada (año real + 30):", reloj_interno.datetime)

# ---------------- LA FOTOCOPIO DE LA ID (certificate pinning) ----------------
# Este firmware no trae lista de autoridades confiables ni deja
# apagar la verificación. Entonces le damos la ÚNICA ID que
# aceptamos: GTS Root R4, la RAÍZ de Google (el "jefe de los
# notarios"), copiada byte por byte del repositorio oficial
# (pki.goog) y verificada: serie, fechas, llave y huella.
# Válida hasta 2036: la placa confía en el jefe, no en un notario
# que rota cada 90 días.
# (Primero intentamos clavar a WE1, un notario, pero su copia
# traía dos letras traspuestas que convertían su fecha de
# vencimiento en "mes 20 de 2029": un mes que no existe. El
# firmware lo botó con INVALID_DATE y culpamos al reloj. Por eso
# clavamos al jefe, que viene de la fuente oficial.)

certificado_raiz_r4 = """-----BEGIN CERTIFICATE-----
MIICCTCCAY6gAwIBAgINAgPlwGjvYxqccpBQUjAKBggqhkjOPQQDAzBHMQswCQYD
VQQGEwJVUzEiMCAGA1UEChMZR29vZ2xlIFRydXN0IFNlcnZpY2VzIExMQzEUMBIG
A1UEAxMLR1RTIFJvb3QgUjQwHhcNMTYwNjIyMDAwMDAwWhcNMzYwNjIyMDAwMDAw
WjBHMQswCQYDVQQGEwJVUzEiMCAGA1UEChMZR29vZ2xlIFRydXN0IFNlcnZpY2Vz
IExMQzEUMBIGA1UEAxMLR1RTIFJvb3QgUjQwdjAQBgcqhkjOPQIBBgUrgQQAIgNi
AATzdHOnaItgrkO4NcWBMHtLSZ37wWHO5t5GvWvVYRg1rkDdc/eJkTBa6zzuhXyi
QHY7qca4R9gq55KRanPpsXI5nymfopjTX15YhmUPoYRlBtHci8nHc8iMai/lxKvR
HYqjQjBAMA4GA1UdDwEB/wQEAwIBhjAPBgNVHRMBAf8EBTADAQH/MB0GA1UdDgQW
BBSATNbrdP9JNqPV2Py1PsVq8JQdjDAKBggqhkjOPQQDAwNpADBmAjEA6ED/g94D
9J+uHXqnLrmvT/aDHQ4thQEd0dlq7A/Cr8deVl5c1RxYIigL9zC2L7F8AjEA8GE8
p/SgguMh1YQdc4acLa/KNJvxn7kjNuK8YAOdgLOaVsjh4rsUecrNIdSUtUlD
-----END CERTIFICATE-----
"""

# Detector de fotocopias torcidas: si estos dos números no coinciden,
# el certificado se corrompió al copiar y pegar. Vuelve a copiarlo.
print("Certificado raíz R4:", len(certificado_raiz_r4),
      "caracteres, checksum",
      sum(ord(caracter) for caracter in certificado_raiz_r4))
# Debe decir: 765 caracteres, checksum 62586

# ---------------- Preparar el botón ----------------

el_boton = digitalio.DigitalInOut(pin_donde_esta_el_boton)
el_boton.direction = digitalio.Direction.INPUT
el_boton.pull = digitalio.Pull.UP
# Con resistencia pull-up:
#   True  = el botón NO está presionado
#   False = el botón SÍ está presionado

# ---------------- Preparar los LEDs ----------------

los_leds = []
for pin_de_un_led in pines_de_los_leds:
    un_led = digitalio.DigitalInOut(pin_de_un_led)
    un_led.direction = digitalio.Direction.OUTPUT
    un_led.value = False  # arrancamos con los LEDs apagados
    los_leds.append(un_led)

# ---------------- Funciones (mis nombres en español) ----------------


def conectar_al_wifi():
    # Si settings.toml ya conectó la placa automáticamente, no lo repetimos:
    if wifi.radio.ipv4_address:
        print("WiFi ya conectado. Dirección IP:", wifi.radio.ipv4_address)
        return
    nombre_de_la_red = os.getenv("WIFI_SSID")
    contrasena_de_la_red = os.getenv("WIFI_PASSWORD")
    print("Conectando a la red:", nombre_de_la_red)
    wifi.radio.connect(nombre_de_la_red, contrasena_de_la_red)
    print("Conectado. Dirección IP de la placa:", wifi.radio.ipv4_address)


def crear_sesion_http():
    pool_de_sockets = socketpool.SocketPool(wifi.radio)
    # El webhook es https: la placa necesita su "kit de seguridad" TLS
    # para hablar cifrado. Sin esto, el POST no puede viajar.
    contexto_seguridad_ssl = ssl.create_default_context()
    # Certificate pinning: la placa solo acepta la raíz R4 que
    # pegamos arriba. Ni impostores ni verificación apagada:
    # revisamos TODO, pero contra la única ID que conocemos.
    contexto_seguridad_ssl.load_verify_locations(cadata=certificado_raiz_r4)
    gc.collect()  # limpiamos la RAM antes de armar la sesión
    sesion_http = adafruit_requests.Session(
        pool_de_sockets, ssl_context=contexto_seguridad_ssl
    )
    return sesion_http


def encender_todos_los_leds():
    for un_led in los_leds:
        un_led.value = True


def apagar_todos_los_leds():
    for un_led in los_leds:
        un_led.value = False


def enviar_pulsacion_post_al_webhook(sesion_http):
    print("Botón presionado. Enviando POST a n8n...")
    encender_todos_los_leds()  # los LEDs se quedan prendidos mientras viaja el POST
    try:
        respuesta = sesion_http.post(
            url_del_webhook_fila_1,
            json={"sumar": 1},
        )
        print("n8n respondió con el código:", respuesta.status_code)
        respuesta.close()  # devolvemos el socket para no quedarnos sin ellos
    finally:
        # Pase lo que pase (éxito o error), los LEDs se apagan:
        apagar_todos_los_leds()


def el_boton_esta_presionado():
    return not el_boton.value  # recordemos: False = presionado (pull-up)


# ---------------- Programa principal ----------------

conectar_al_wifi()
sesion_http = crear_sesion_http()
print("Listo. Presiona el botón para sumar.")

el_boton_estaba_presionado = False

while True:
    el_boton_esta_presionado_ahora = el_boton_esta_presionado()
    acaba_de_presionarse_el_boton = (
        el_boton_esta_presionado_ahora and not el_boton_estaba_presionado
    )
    el_boton_estaba_presionado = el_boton_esta_presionado_ahora

    if acaba_de_presionarse_el_boton:
        try:
            enviar_pulsacion_post_al_webhook(sesion_http)
        except OSError as error_de_red:
            print("Falló el POST. Revisa el WiFi o la URL:", error_de_red)
