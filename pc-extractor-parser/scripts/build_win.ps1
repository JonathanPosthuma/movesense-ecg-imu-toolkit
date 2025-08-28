pyinstaller --noconfirm --clean --windowed `
  --name "MovesenseWin" `
  --icon pc-extractor-parser\icons\app.ico `
  --add-data "pc-extractor-parser\gui;gui" `
  --add-data "pc-extractor-parser\icons;icons" `
  --add-data "pc-extractor-parser\platform_support;platform_support" `
  --collect-all winrt `
  --collect-all bleak_winrt `
  pc-extractor-parser\gui\main_window.py