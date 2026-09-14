from aiohttp import web

# NOTE: bot.py does `runner = web.AppRunner(await web_server())`, so this
# function MUST return an aiohttp.web.Application instance. It must NOT start
# its own runner/site (bot.py does that) or the AppRunner receives None and
# fails with "The first argument should be web.Application instance, got None".

async def handle(request):
    return web.Response(text="Bot is running and healthy!")

async def web_server():
    app = web.Application()
    app.add_routes([web.get('/', handle)])
    return app
