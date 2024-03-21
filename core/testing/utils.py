


import os
import xml.etree.ElementTree as ET
import cairosvg
from PIL import Image

def render_pgx_2p(frames, p_ids, title, frame_dir, p1_label='black', p2_label='white', duration=900):
    digit_length = len(str(len(frames)))
    trained_agent_color = p1_label if p_ids[0] == 0 else p2_label
    opponent_color = p2_label if trained_agent_color == p1_label else p2_label
    p1_win = False
    p2_win = False
    draw = False
    images = []
    for i,frame in enumerate(frames):
        env_state = frame.env_state
        if frame.completed.item():
            num = '9' * digit_length
            env_state.save_svg(f"{frame_dir}/{num}.svg", color_theme='dark')
            p1_win = frame.outcomes[0] > frame.outcomes[1]
            p2_win = frame.outcomes[1] > frame.outcomes[0]
            draw = frame.outcomes[0] == frame.outcomes[1]
    
        else:
            num = str(i).zfill(digit_length)
            env_state.save_svg(f"{frame_dir}/{num}.svg", color_theme='dark')
        
        tree = ET.parse(f"{frame_dir}/{num}.svg")
        root = tree.getroot()

        viewBox = root.attrib.get('viewBox', None)
        if viewBox:
            viewBox = viewBox.split()
            viewBox = [float(v) for v in viewBox]
            original_height = viewBox[3]
        else:
            original_width = float(root.attrib.get('width', 0))
            original_height = float(root.attrib.get('height', 0))

        new_height = original_height * 1.2
        # Update the viewBox and height attributes
        if viewBox:
    
            viewBox[3] = new_height
            root.attrib['viewBox'] = ' '.join(map(str, viewBox))
        root.attrib['height'] = str(new_height)

        # Create a new text element
        p1_text = ET.Element('ns0:text', x=str(0.01 * original_width), y=str(original_height * 1.05), fill='white', style='font-family: Arial;')
        p2_text = ET.Element('ns0:text', x=str(0.01 * original_width), y=str(original_height * 1.15), fill='white', style='font-family: Arial;')
        emoji = "[W]" if p1_win else "[L]" if p2_win else "[D]" if draw else ""
        agent_text = f"{emoji} Trained Agent ({trained_agent_color}): {'+' if frame.p1_value_estimate > 0 else ''}{frame.p1_value_estimate:.4f}"
        emoji = "[W]" if p2_win else "[L]" if p1_win else "[D]" if draw else ""
        opp_text = f"{emoji} Opponent ({opponent_color}): {'+' if frame.p2_value_estimate > 0 else ''}{frame.p2_value_estimate:.4f}"
        p1_text.text = agent_text
        p2_text.text = opp_text

        new_area = ET.Element('ns0:rect', fill='#1e1e1e', height=str(new_height - original_height), width=str(original_width), x='0', y=str(original_height))
        root.append(new_area)

        # Add the text element to the SVG
        root.append(p1_text)
        root.append(p2_text)
    
        

        # Save the modified SVG
        tree.write(f"{frame_dir}/{num}.svg", encoding='utf-8', xml_declaration=True)

        cairosvg.svg2png(url=f"{frame_dir}/{num}.svg", write_to=f"{frame_dir}/{num}.png")
        images.append(f"{frame_dir}/{num}.png")
        if frame.completed.item():
            break


    # Read the PNG images into a list of Image objects
    images = [Image.open(png) for png in images]

    # Save the images as a GIF
    gif_path = f"{frame_dir}/{title}.gif"
    images[0].save(gif_path, save_all=True, append_images=images[1:] + ([images[-1]] * 2), duration=duration, loop=0)
    
    os.system(f"rm {frame_dir}/*.svg")
    os.system(f"rm {frame_dir}/*.png")
    return gif_path