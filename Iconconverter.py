from PIL import Image

def create_icon(input_path, output_path, sizes=(16, 32, 48, 64, 128, 256)):
    image = Image.open(input_path)
    icon_sizes = [(size, size) for size in sizes]
    image.save(output_path, format='ICO', sizes=icon_sizes)

create_icon('ICON program.png', 'output_icon.ico')
