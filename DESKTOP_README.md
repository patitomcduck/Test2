# Collector POS Desktop Preview 3.0

Esta rama convierte Collector POS en una aplicación de escritorio para Windows sin depender de Docker para el cliente final.

## Arquitectura

- `CollectorPOS.exe` inicia el backend Flask de forma local y abre una ventana nativa con pywebview.
- Los datos viven fuera del programa en `%LOCALAPPDATA%\CollectorPOS`.
- La pantalla de cliente sigue disponible por LAN en `http://IP-DE-LA-PC:8765/cliente`.
- El instalador crea tareas de Windows para actualizar precios a las 00:00, 12:00 y 17:00 y un respaldo a las 02:30.
- Desinstalar la aplicación **no borra** la base de datos de la tienda.

## Build de Windows

Se recomienda Python 3.12 x64 para el build de referencia.

1. Instala Python 3.12 x64.
2. Instala Inno Setup 6 si quieres generar el instalador final.
3. Ejecuta PowerShell en esta carpeta:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build-windows.ps1
```

El ejecutable portable queda en:

`dist\CollectorPOS\CollectorPOS.exe`

Si Inno Setup está instalado, el instalador queda en:

`dist-installer\CollectorPOS-Setup-3.0.0-preview.exe`

## Configurar JustTCG en una PC piloto

Antes del primer uso puedes ejecutar:

```powershell
.\configure-desktop.ps1
```

La clave queda en `%LOCALAPPDATA%\CollectorPOS\collector.env`, no dentro del instalador.

## Importante

Esta es una **Desktop Preview**, no la build comercial firmada. El siguiente paso será automatizar el build en Windows, firma de código, actualización automática y activación de licencias.

## Datos y desinstalación

La base de datos y configuración viven en `%LOCALAPPDATA%\CollectorPOS`. El desinstalador no elimina esa carpeta para evitar borrar accidentalmente información de la tienda.

## Build automático con GitHub Actions

El proyecto incluye `.github/workflows/build-windows.yml`. Al subirlo a GitHub puedes ejecutar **Build Collector POS Windows** manualmente y descargar el `Setup.exe` como artifact del workflow.
