# Collector POS 2.2 - Distribución limpia con JustTCG

Esta es la copia maestra limpia para nuevas tiendas. No contiene inventario, ventas, usuarios, logos, redes sociales ni datos de identificación de otra tienda.

## JustTCG

La integración con JustTCG viene incluida. Durante la instalación, el script te pide la API key y la guarda únicamente en el archivo `.env` local de esa instalación. La clave NO viene embebida dentro del ZIP.

- Pokémon: TCGdex como fuente principal + JustTCG como respaldo cuando falta precio.
- One Piece, Magic, Yu-Gi-Oh!, Lorcana y otros TCG soportados: JustTCG.
- Actualizador de precios: utiliza la misma clave en el contenedor `price-updater`.

## Instalación limpia

```bash
unzip -o collector-pos-v2.2-clean-justtcg.zip
cd collector-pos-v2.2-clean-justtcg
chmod +x install-clean.sh
./install-clean.sh
```

El instalador preguntará por tu API key JustTCG sin mostrarla en pantalla.

Por seguridad, la instalación predeterminada se crea en `~/docker/collector-pos-clean` y usa el puerto `8089`.

Después abre:

```text
http://IP-DEL-EQUIPO:8089
```

## Cambiar JustTCG después

Dentro de la instalación puedes ejecutar:

```bash
./configure-justtcg.sh
```

El script guarda la nueva clave en `.env` y reinicia el POS y el actualizador de precios.

## Funciones incluidas

POS, inventario, TCGdex/JustTCG, actualización automática de precios, turnos, modo evento, intercambios, pantalla de cliente, usuarios/PIN, devoluciones y personalización white-label.
