from pathlib import Path


def write_move_history(file_name, title, result_text, snapshots):
    from email import encoders
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.charset import Charset, QP
    from email.policy import SMTP
    from email.utils import formatdate
    import base64
    from html import escape

    sections = [
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>{escape(title)} move history</title></head>'
        '<body style="font-family:sans-serif;background:#f5f7fa;color:#17202a">',
        f'<h1>{escape(title)}</h1><h2>{escape(result_text)}</h2>',
    ]
    for _, svg, description in snapshots:
        image_data = base64.b64encode(svg.encode('utf-8')).decode('ascii')
        sections.append(
            f'<section><h3>{escape(description)}</h3>'
            f'<img src="data:image/svg+xml;base64,{image_data}" '
            f'alt="{escape(description)}" style="width:100%;max-width:560px;height:auto"></section>'
        )
    sections.append('</body></html>')
    html = ''.join(sections)
    output_path = Path(file_name).expanduser().resolve()
    root_location = output_path.as_uri()
    message = MIMEMultipart('related', type='text/html')
    message['From'] = '<Saved by Blink>'
    message['Snapshot-Content-Location'] = root_location
    message['Subject'] = f'{title} move history'
    message['Date'] = formatdate(localtime=False)
    html_charset = Charset('utf-8')
    html_charset.body_encoding = QP
    root_part = MIMEText(html, 'html', html_charset)
    del root_part['MIME-Version']
    root_part['Content-Location'] = root_location
    message.attach(root_part)
    for content_id, svg, _ in snapshots:
        part = MIMEBase('image', 'svg+xml')
        del part['MIME-Version']
        part.set_payload(svg.encode('utf-8'))
        encoders.encode_base64(part)
        part.add_header('Content-ID', f'<{content_id}>')
        image_location = output_path.with_name(
            f'{output_path.stem}-{content_id}.svg'
        ).as_uri()
        part.add_header('Content-Location', image_location)
        message.attach(part)
    output_path.write_bytes(message.as_bytes(policy=SMTP))
    return output_path
