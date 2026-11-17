"""Instalación opcional de los sensores de CPU (potencia y reloj) con LibreHardwareMonitor.

No se distribuyen binarios en este repositorio. Con permiso del usuario se descargan de NuGet (nuget.org) dos paquetes con la versión
fijada y se comprueba el SHA-256 del paquete y de cada DLL antes de copiarla a lib\\. Si algo no coincide, no se instala nada.

  LibreHardwareMonitorLib 0.9.6 (MPL-2.0) https://github.com/LibreHardwareMonitor/LibreHardwareMonitor
  HidSharp 2.6.4 (ver su LICENSE.txt)       https://www.nuget.org/packages/HidSharp
"""
import hashlib
import io
import os
import urllib.request
import zipfile

NUGET = "https://api.nuget.org/v3-flatcontainer"
PAQUETES = [
    {
        "url": f"{NUGET}/librehardwaremonitorlib/0.9.6/librehardwaremonitorlib.0.9.6.nupkg",
        "sha256": "60db5e0d9135f4658b385cae139d3b9eaeac0a8e867d322d20b5dbe4c4039197",
        "extraer": {
            "runtimes/win-x64/lib/net472/LibreHardwareMonitorLib.dll":
                "6ebc194316536ba61af5be24508ad9fcbb2ecc685e716c12e787c79530f66bf0",
        },
    },
    {
        "url": f"{NUGET}/hidsharp/2.6.4/hidsharp.2.6.4.nupkg",
        "sha256": "cf55d4b4d7b42dc1d8496b7b592ba00d1ecd48a70bc07bd8dabd3e9976d108e7",
        "extraer": {
            "lib/net35/HidSharp.dll": "d86690efde30ea9179f669320f39148853793b743a98b531afeaf30598e22f54",
        },
    },
]
NECESARIAS = ("LibreHardwareMonitorLib.dll", "HidSharp.dll")

AVISO = (
    "Para mostrar la potencia y el reloj de la CPU el gadget necesita LibreHardwareMonitor.\n\n"
    "¿Descargar ahora, de nuget.org, LibreHardwareMonitorLib 0.9.6 (licencia MPL-2.0) y HidSharp 2.6.4?\n"
    "Se comprueba su huella SHA-256 antes de instalarlas. El gadget se ejecuta como administrador, "
    "así que solo acepta si te fías de estas fuentes. Puedes decir que no: el resto del gadget funciona igual."
)


def instaladas(lib: str) -> bool:
    return all(os.path.exists(os.path.join(lib, n)) for n in NECESARIAS)


def instalar(lib: str) -> tuple[bool, str]:
    """Descarga y verifica los dos paquetes; solo escribe en lib si TODO coincide. Devuelve (ok, mensaje)."""
    try:
        listos = {}
        for p in PAQUETES:
            req = urllib.request.Request(p["url"], headers={"User-Agent": "hwpill_gadget"})
            datos = urllib.request.urlopen(req, timeout=60).read()
            if hashlib.sha256(datos).hexdigest() != p["sha256"]:
                return False, "La descarga no coincide con la huella esperada. No se ha instalado nada."
            z = zipfile.ZipFile(io.BytesIO(datos))
            for interno, esperado in p["extraer"].items():
                dll = z.read(interno)
                if hashlib.sha256(dll).hexdigest() != esperado:
                    return False, "Una DLL no coincide con la huella esperada. No se ha instalado nada."
                listos[os.path.basename(interno)] = dll
        os.makedirs(lib, exist_ok=True)
        for nombre, dll in listos.items():
            with open(os.path.join(lib, nombre), "wb") as f:
                f.write(dll)
        return True, "Sensores instalados."
    except Exception as e:  # sin red, nuget caído, etc.
        return False, f"No se pudo descargar ({type(e).__name__}). Inténtalo más tarde desde el menú."
